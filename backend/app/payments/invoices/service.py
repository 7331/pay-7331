"""InvoiceService: the request-side use cases of invoices (PLAN.md sections 4, 6 and 7), on one session.

Making an invoice is ``invoice_creation.py``'s and the payer's pick of a (chain, asset) is ``invoice_asset_choice.py``'s
(PLAN.md section 15); this class puts them, the reads, the account actions and the refunds behind one object. Every write
locks the invoice row (``FOR UPDATE``), as the watcher does, so the two never interleave on one invoice. Routes and the
CLI call this; the watcher uses ``chain_readings_service.py``.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Self

from eth_utils import to_checksum_address
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.payments.account_events.service import AccountEventService
from app.payments.accounts.preferences_service import AccountPreferencesService
from app.payments.chains.types import AssetConfiguration, ChainConfiguration, MoneroChainConfiguration
from app.payments.invoices.creation_limit_repository import InvoiceCreationLimitRepository
from app.payments.invoices.creation_limits import InvoiceCreationLimits
from app.payments.invoices.enums import InvoiceCreationChannel, InvoiceEventType, InvoiceStatus
from app.payments.invoices.errors import (
    FactoryNotDeployedError,
    InvoiceNotFoundError,
    InvoiceStateError,
    RefundNotAllowedError,
)
from app.payments.invoices.evm_deposit_targets import ZERO_ADDRESS, EvmDepositTargets
from app.payments.invoices.invoice_asset_choice import InvoiceAssetChoice
from app.payments.invoices.invoice_creation import InvoiceCreation
from app.payments.invoices.log_events import (
    InvoiceStatusChanged,
    OverpayRefundRecorded,
    StrayRefundRecorded,
)
from app.payments.invoices.models import Invoice, offered_assets_by_chain_of, require_deposit_address
from app.payments.invoices.monero_payment_rules import is_in_block, is_on_its_way
from app.payments.invoices.payment_options import (
    find_chain_prices,
    find_prices_by_chain,
    present_payment_options,
)
from app.payments.invoices.presenters import (
    InvoiceSnapshot,
    compute_refundable_split,
    compute_stray_refundables,
    decode_cursor,
    encode_cursor,
    has_usd_price,
    is_asset_amount_at_least_minimum,
    is_at_least_minimum,
    is_underpaid_within_payment_window,
    list_refunds_in_asset,
    pays_the_operator,
    present_account_invoice,
    present_fiat_amount,
    present_public_invoice,
    render_qr_svg,
)
from app.payments.invoices.repository import InvoiceRepository
from app.payments.invoices.schemas import (
    InvoiceAccountResponse,
    InvoiceFiat,
    InvoicePageResponse,
    InvoicePublicResponse,
    PaymentOption,
    RefundOfferItemResponse,
    RefundOfferListResponse,
)
from app.payments.invoices.types import NewInvoice, PaymentLinkInvoiceCounts
from app.payments.invoices.wiring import InvoiceWiring
from app.payments.monero_wallets.service import MoneroWalletService
from app.payments.price_quotes.errors import PriceUnavailableError
from app.payments.price_quotes.service import PriceQuoteService
from app.payments.price_quotes.types import ChainPrices
from app.payments.settlements.enums import OperationStatus
from app.payments.settlements.service import SettlementService
from app.payments.settlements.types import RefundOfferDraft
from app.shared import logging as log
from app.shared.amounts import format_base_units, parse_decimal_amount, to_base_units
from app.shared.errors import InvalidInputError

logger = log.get_logger(__name__)

INVOICES_PER_PAGE = 50


async def is_invoice_deposit_address(session: AsyncSession, *, address: str) -> bool:
    """Whether an invoice's deposit is ``address``: half of the gateway's deposit address check."""
    return await InvoiceRepository(session=session).exists_deposit_address(address)


class InvoiceService:
    def __init__(
        self,
        *,
        repository: InvoiceRepository,
        wiring: InvoiceWiring,
        price_quote_service: PriceQuoteService,
        settlement_service: SettlementService,
        account_event_service: AccountEventService,
        account_preferences_service: AccountPreferencesService,
        monero_wallet_service: MoneroWalletService,
    ) -> None:
        self.repository = repository
        self.monero_wallet_service = monero_wallet_service
        self.wiring = wiring
        self.chains_service = wiring.chains_service
        self.price_quote_service = price_quote_service
        self.settlement_service = settlement_service
        self.account_event_service = account_event_service
        self.account_preferences_service = account_preferences_service
        self.evm_deposit_targets = EvmDepositTargets(repository=repository, wiring=wiring)
        self.invoice_creation = InvoiceCreation(
            repository=repository,
            wiring=wiring,
            price_quote_service=price_quote_service,
            monero_wallet_service=monero_wallet_service,
            creation_limits=InvoiceCreationLimits(
                repository=InvoiceCreationLimitRepository(session=repository.session),
                invoice_settings=wiring.invoice_settings,
                ip_hash_salt=wiring.screening_settings.ip_hash_salt,
            ),
            evm_deposit_targets=self.evm_deposit_targets,
            account_preferences_service=account_preferences_service,
        )
        self.invoice_asset_choice = InvoiceAssetChoice(
            wiring=wiring, invoice_creation=self.invoice_creation, evm_deposit_targets=self.evm_deposit_targets
        )

    @classmethod
    def on_session(cls, session: AsyncSession, *, redis: Redis, wiring: InvoiceWiring) -> Self:
        return cls(
            repository=InvoiceRepository(session=session),
            wiring=wiring,
            price_quote_service=PriceQuoteService.on_session(
                session, redis=redis, chains_service=wiring.chains_service, chainlink_clients=wiring.chainlink_clients
            ),
            settlement_service=SettlementService.on_session(
                session, operator_key_addresses=wiring.operator_key_addresses
            ),
            account_event_service=AccountEventService.on_session(session),
            account_preferences_service=AccountPreferencesService.on_session(session),
            monero_wallet_service=MoneroWalletService.on_session(
                session,
                chains_service=wiring.chains_service,
                monero_sidecar=wiring.monero_sidecar,
                private_view_key_encryption_key=wiring.monero_private_view_key_encryption_key,
            ),
        )

    # Reads

    def offered_assets_of_invoice(self, invoice: Invoice) -> dict[str, list[str]]:
        """The exact coins creation accepted per network, for the account's next invoice defaults."""
        return offered_assets_by_chain_of(invoice)

    async def get_invoice(self, invoice_id: str, *, lock: bool = False) -> Invoice:
        invoice = await self.repository.find_invoice(invoice_id, lock=lock)
        if invoice is None:
            raise InvoiceNotFoundError("No invoice with that id.")
        return invoice

    async def find_chain_prices(self, chain_key: str) -> ChainPrices | None:
        return await find_chain_prices(chain_key, price_quote_service=self.price_quote_service)

    async def load_snapshot(self, invoice: Invoice) -> InvoiceSnapshot:
        chain = self.chains_service.get_chain(invoice.chain_key)
        prices = await self.find_chain_prices(invoice.chain_key)
        offered_chains: dict[str, ChainConfiguration] = {}
        option_prices_by_chain: dict[str, ChainPrices] = {}
        if invoice.status == InvoiceStatus.AWAITING_ASSET:
            # Each offered chain priced on its own: one chain's feeds failing leaves the others' options intact.
            other_chain_keys = [key for key in offered_assets_by_chain_of(invoice) if key != invoice.chain_key]
            offered_chains = {key: self.chains_service.get_chain(key) for key in offered_assets_by_chain_of(invoice)}
            option_prices_by_chain = await find_prices_by_chain(
                other_chain_keys, price_quote_service=self.price_quote_service
            )
            if prices is not None:
                option_prices_by_chain[invoice.chain_key] = prices
        return InvoiceSnapshot(
            invoice=invoice,
            chain=chain,
            monero_payments=await self.repository.list_monero_payments([invoice.id])
            if isinstance(chain, MoneroChainConfiguration)
            else [],
            payments=await self.repository.list_payments([invoice.id]),
            stray_payments=await self.repository.list_stray_payments([invoice.id]),
            sweep_events=await self.repository.list_sweep_events(
                invoice.chain_key,
                deposit_addresses=[invoice.deposit_address] if invoice.deposit_address is not None else [],
            ),
            operations=await self.settlement_service.list_operations_for_invoices([invoice.id]),
            latest_quote=await self.price_quote_service.find_latest_quote(invoice.id),
            prices=prices,
            offered_chains=offered_chains,
            option_prices_by_chain=option_prices_by_chain,
            minimum_refund_usd=self.wiring.invoice_settings.minimum_refund_usd,
            public_base_url=self.wiring.public_base_url,
            float_refunds_allowed=self.allows_float_refunds(invoice),
            seller_support_contact=await self.account_preferences_service.find_seller_support_contact(
                invoice.account_id
            ),
        )

    def is_monero_invoice(self, invoice: Invoice) -> bool:
        return isinstance(self.chains_service.get_chain(invoice.chain_key), MoneroChainConfiguration)

    def allows_float_refunds(self, invoice: Invoice) -> bool:
        """Never on Monero: nothing can go back there (ring signatures hide the sender; PLAN.md section 14)."""
        invoice_settings = self.wiring.invoice_settings
        return not self.is_monero_invoice(invoice) and pays_the_operator(
            invoice,
            operator_account_ids=invoice_settings.operator_account_ids,
            operator_destination_address=invoice_settings.default_destination_address,
        )

    async def get_account_invoice(self, invoice_id: str, *, account_id: int, lock: bool = False) -> Invoice:
        """The account's own invoice; another's (or one made signed out) is not found, as if it did not exist."""
        invoice = await self.get_invoice(invoice_id, lock=lock)
        if invoice.account_id != account_id:
            raise InvoiceNotFoundError("No invoice with that id.")
        return invoice

    async def read_account_invoice(self, invoice_id: str, *, account_id: int) -> InvoiceAccountResponse:
        invoice = await self.get_account_invoice(invoice_id, account_id=account_id)
        return present_account_invoice(await self.load_snapshot(invoice))

    async def read_public_invoice(self, invoice_id: str) -> InvoicePublicResponse:
        return present_public_invoice(await self.load_snapshot(await self.get_invoice(invoice_id)))

    async def list_account_invoices(
        self,
        account_id: int,
        *,
        status: InvoiceStatus | None,
        cursor: str | None,
        order_id: str | None = None,
        order_id_prefix: str | None = None,
    ) -> InvoicePageResponse:
        """The account's invoices, newest first, 50 a page; with ``order_id`` only the one invoice it names (0 or 1
        items: ``order_id`` is unique per account), so a server can find what it made (2026-10-12); with
        ``order_id_prefix`` those whose ``order_id`` starts with it, so a server lists one customer's (2026-10-15)."""
        if order_id is not None and order_id_prefix is not None:
            raise InvalidInputError("order_id_prefix: give order_id or order_id_prefix, not both")
        if order_id is not None:
            found = await self.repository.find_account_invoice_by_order_id(order_id, account_id=account_id)
            found_invoices = [found] if found is not None and (status is None or found.status == status) else []
            return InvoicePageResponse(
                invoices=[present_account_invoice(await self.load_snapshot(invoice)) for invoice in found_invoices],
                next_cursor=None,
            )
        # One row past the page: a cursor only when an older page has rows, so "Load older" never reads an empty one.
        invoices = await self.repository.list_account_invoices_page(
            account_id,
            status=status,
            order_id_prefix=order_id_prefix,
            after=decode_cursor(cursor) if cursor else None,
            limit=INVOICES_PER_PAGE + 1,
        )
        page_invoices = invoices[:INVOICES_PER_PAGE]
        return InvoicePageResponse(
            invoices=[present_account_invoice(await self.load_snapshot(invoice)) for invoice in page_invoices],
            next_cursor=encode_cursor(page_invoices[-1]) if len(invoices) > INVOICES_PER_PAGE else None,
        )

    # Quantity links (payments/payment_links reads its invoices only through these)

    async def count_payment_link_invoices(self, payment_link_ids: list[str]) -> dict[str, PaymentLinkInvoiceCounts]:
        """Every listed link's counts, zero for a link with no invoice yet."""
        counts = await self.repository.count_payment_link_invoices(payment_link_ids)
        return {
            payment_link_id: PaymentLinkInvoiceCounts(
                slots_held=counts.get(payment_link_id, (0, 0))[0], paid=counts.get(payment_link_id, (0, 0))[1]
            )
            for payment_link_id in payment_link_ids
        }

    async def list_payment_link_invoices(
        self, payment_link_id: str, *, account_id: int, limit: int
    ) -> list[InvoiceAccountResponse]:
        """The link's invoices, newest first, as the account sees them; only the account's own."""
        invoices = await self.repository.list_payment_link_invoices(payment_link_id, limit=limit)
        return [
            present_account_invoice(await self.load_snapshot(invoice))
            for invoice in invoices
            if invoice.account_id == account_id
        ]

    # Invoice schedules (payments/invoice_schedules reads its invoices only through this)

    async def list_invoice_schedule_invoices(
        self, invoice_schedule_id: str, *, account_id: int, limit: int
    ) -> list[InvoiceAccountResponse]:
        """The schedule's invoices, newest period first, as the account sees them; only the account's own."""
        invoices = await self.repository.list_invoice_schedule_invoices(invoice_schedule_id, limit=limit)
        return [
            present_account_invoice(await self.load_snapshot(invoice))
            for invoice in invoices
            if invoice.account_id == account_id
        ]

    async def invoice_schedule_period_is_billed(self, invoice_schedule_id: str, *, period_start: datetime) -> bool:
        return await self.repository.invoice_schedule_period_is_billed(invoice_schedule_id, period_start=period_start)

    async def account_order_id_is_used(self, order_id: str, *, account_id: int) -> bool:
        """An invoice of the account (its current one, after refreshes) already has this order id."""
        return await self.repository.find_account_invoice_by_order_id(order_id, account_id=account_id) is not None

    async def read_fiat_payment_options(
        self,
        fiat_amount: Decimal,
        *,
        fiat_currency: str,
        legacy_chain_key: str,
        offered_assets_by_chain: dict[str, list[str]],
    ) -> tuple[InvoiceFiat, list[PaymentOption]]:
        """A fiat amount's USD and EUR worth (by the first offered chain's feeds) and every offered chain's options
        (not quotes); each chain is priced on its own, so a chain without a feed has options with no amount and leaves
        the rest alone."""
        first_chain_key = legacy_chain_key
        prices_by_chain = await find_prices_by_chain(
            list(offered_assets_by_chain), price_quote_service=self.price_quote_service
        )
        first_prices = prices_by_chain.get(first_chain_key)
        fiat = present_fiat_amount(
            fiat_amount,
            fiat_currency=fiat_currency,
            eur_price_usd=first_prices.eur_price_usd if first_prices else None,
        )
        chains_by_key = {key: self.chains_service.get_chain(key) for key in offered_assets_by_chain}
        return (
            fiat,
            present_payment_options(
                chains_by_key,
                offered_assets_by_chain=offered_assets_by_chain,
                prices_by_chain=prices_by_chain,
                fiat_amount=fiat_amount,
                fiat_currency=fiat_currency,
            ),
        )

    async def render_invoice_qr_svg(self, invoice_id: str, *, address_only: bool) -> str:
        view = await self.read_public_invoice(invoice_id)
        text = view.deposit_address if address_only else view.payment_uri
        if text is None:
            raise InvoiceStateError("the invoice has no payment address yet; pick an asset first")
        return render_qr_svg(text)

    # Making invoices

    async def sum_finalized_deposit_sweeps(
        self, chain_key: str, *, deposit_addresses: list[str], up_to_block_number: int
    ) -> dict[tuple[str, str | None], int]:
        """What the factory's stored ``DepositSwept`` events moved out of each deposit per token (None for the native
        coin) at or before the block, whoever swept: the swept part of a deposit's gross (PLAN.md section 4). Static
        deposit addresses read theirs here (PLAN.md section 17); the keys are lowercased addresses."""
        swept_by_deposit_and_token: dict[tuple[str, str | None], int] = {}
        for deposit_address, token_address, total in await self.repository.sum_sweep_events_up_to_block(
            chain_key, deposit_addresses=deposit_addresses, up_to_block_number=up_to_block_number
        ):
            key = (deposit_address.lower(), token_address.lower() if token_address else None)
            swept_by_deposit_and_token[key] = swept_by_deposit_and_token.get(key, 0) + total
        return swept_by_deposit_and_token

    def list_invoiceable_evm_chain_keys(self) -> list[str]:
        """The enabled EVM chains with a deployed factory, in ``PAY_ENABLED_CHAINS`` order (signed-out invoices stay EVM,
        PLAN.md section 14): the chains `/`'s one-tap invoices offer (PLAN.md section 15)."""
        chain_keys = [chain.key for chain in self.chains_service.evm_chains if chain.key in self.wiring.factory_clients]
        if not chain_keys:
            raise FactoryNotDeployedError("no deposit factory is configured on any enabled chain yet")
        return chain_keys

    async def create_invoice(
        self,
        new_invoice: NewInvoice,
        *,
        client_ip_address: str | None,
        creation_channel: InvoiceCreationChannel = InvoiceCreationChannel.WEB,
    ) -> Invoice:
        return await self.invoice_creation.create_invoice(
            new_invoice, client_ip_address=client_ip_address, creation_channel=creation_channel
        )

    # Account actions

    async def change_status(self, invoice: Invoice, *, status: InvoiceStatus, event_type: InvoiceEventType) -> None:
        log.emit(
            logger,
            event=InvoiceStatusChanged(
                invoice_id=invoice.id, previous_status=invoice.status.value, status=status.value
            ),
        )
        invoice.status = status
        await self.record_invoice_event(invoice, event_type=event_type)

    async def record_invoice_event(self, invoice: Invoice, *, event_type: InvoiceEventType) -> None:
        """An account event on this session, so it commits with the change it describes; an invoice made signed out
        has no account, and its event is kept but streamed to nobody."""
        await self.repository.session.flush()
        await self.repository.session.refresh(invoice)
        view_json = present_account_invoice(await self.load_snapshot(invoice)).model_dump(mode="json")
        await self.account_event_service.record_account_event(
            invoice.id, account_id=invoice.account_id, event_type=event_type.value, data=view_json
        )

    async def cancel_invoice(self, invoice_id: str, *, account_id: int) -> InvoiceAccountResponse:
        invoice = await self.get_account_invoice(invoice_id, account_id=account_id, lock=True)
        cancellable = {InvoiceStatus.AWAITING_ASSET, InvoiceStatus.AWAITING_PAYMENT}
        if invoice.status not in cancellable or invoice.received_base_units or invoice.latest_gross_base_units:
            raise InvoiceStateError("only an invoice with nothing received can be cancelled")
        if isinstance(self.chains_service.get_chain(invoice.chain_key), MoneroChainConfiguration):
            now = datetime.now(UTC)
            payment_rows = await self.repository.list_monero_payments([invoice.id], lock=True)
            if any(is_in_block(row) or is_on_its_way(row, now=now) for row in payment_rows):
                raise InvoiceStateError("an invoice with a payment on its way cannot be cancelled")
        invoice.closed_at = datetime.now(UTC)
        await self.change_status(invoice, status=InvoiceStatus.CANCELLED, event_type=InvoiceEventType.CANCELLED)
        return await self.read_account_invoice(invoice_id, account_id=account_id)

    async def accept_partial(self, invoice_id: str, *, account_id: int) -> InvoiceAccountResponse:
        """The account takes an underpaid amount as paid; the watcher then sweeps what the deposit holds at finality
        (or closes it when empty: a third party may have swept it already, 2026-10-09)."""
        invoice = await self.get_account_invoice(invoice_id, account_id=account_id, lock=True)
        if invoice.status != InvoiceStatus.UNDERPAID:
            raise InvoiceStateError("only an underpaid invoice can be accepted as partially paid")
        snapshot = await self.load_snapshot(invoice)
        # A stray coin sent back is not this invoice's money going back (INC-001).
        if any(
            operation.status != OperationStatus.FAILED
            for operation in list_refunds_in_asset(snapshot.operations, asset_symbol=invoice.asset_symbol)
        ):
            # Its money is being or was given back: paid it is not (2026-10-09).
            raise InvoiceStateError("a refund was made on this invoice; it cannot be accepted as partially paid")
        invoice.partial = True
        invoice.paid_at = datetime.now(UTC)
        invoice.amount_due_base_units = invoice.received_base_units
        if self.is_monero_invoice(invoice):
            # The money is in the receiver's wallet already: nothing is swept, so it closes now.
            invoice.closed_at = invoice.paid_at
        await self.change_status(invoice, status=InvoiceStatus.PAID, event_type=InvoiceEventType.PAID)
        return await self.read_account_invoice(invoice_id, account_id=account_id)

    async def request_sweep(self, invoice: Invoice, *, skipped_token_addresses: Sequence[str] = ()) -> bool:
        """False on Monero, where the payer pays the receiver's own wallet and there is nothing to sweep."""
        if self.is_monero_invoice(invoice):
            return False
        return await self.settlement_service.request_sweep(
            invoice.id,
            chain_key=invoice.chain_key,
            deposit_address=require_deposit_address(invoice),
            invoice_salt=invoice.invoice_salt,
            destination_address=invoice.destination_address,
            skipped_token_addresses=skipped_token_addresses,
        )

    async def request_sweep_of_a_closing_deposit(self, invoice: Invoice, *, asset: AssetConfiguration) -> None:
        """Sweep a deposit being closed only when its last reading holds at least the minimum sweep: a third party may
        have swept it already (2026-10-09, live: a sweep of an empty deposit paid gas for nothing). Money that comes
        later is the rescan's."""
        if invoice.latest_balance_base_units >= asset.minimum_sweep_base_units:
            await self.request_sweep(invoice)

    async def sweep_invoice_by_hand(self, invoice_id: str, *, skipped_asset_symbols: Sequence[str] = ()) -> bool:
        """The CLI's ``sweep-invoice``: clear ``sweep_blocked`` and record a sweep, leaving out the configured tokens
        ``skipped_asset_symbols`` names (the contract always forwards the native coin); False when one is active."""
        invoice = await self.get_invoice(invoice_id, lock=True)
        self.chains_service.get_evm_chain(invoice.chain_key)
        skipped_assets = [
            self.chains_service.get_asset(invoice.chain_key, symbol=symbol) for symbol in skipped_asset_symbols
        ]
        if any(asset.token_address is None for asset in skipped_assets):
            raise InvalidInputError("skip-token: the contract always forwards the native coin; it cannot be skipped")
        invoice.sweep_blocked = False
        return await self.request_sweep(
            invoice, skipped_token_addresses=[asset.token_address for asset in skipped_assets if asset.token_address]
        )

    # Payer actions

    async def choose_asset(self, invoice_id: str, *, chain_key: str | None, asset_symbol: str) -> InvoicePublicResponse:
        """Pick the (chain, asset) of an ``awaiting_asset`` invoice: starts its quote and its lifetime. ``chain_key`` may
        be left out only when the coin is offered on one chain."""
        invoice = await self.get_invoice(invoice_id, lock=True)
        await self.invoice_asset_choice.choose_asset(invoice, chain_key=chain_key, asset_symbol=asset_symbol)
        await self.repository.session.flush()
        return await self.read_public_invoice(invoice_id)

    async def refresh_quote(self, invoice_id: str) -> InvoicePublicResponse:
        """A fresh quote for the remainder of an underpaid fiat invoice whose quote lapsed (PLAN.md section 4)."""
        invoice = await self.get_invoice(invoice_id, lock=True)
        if (
            invoice.status != InvoiceStatus.UNDERPAID
            or invoice.closed_at is not None
            or invoice.fiat_amount is None
            or invoice.fiat_currency is None
            or invoice.asset_symbol is None
        ):
            raise InvoiceStateError("only an open underpaid fiat invoice gets a fresh quote")
        # A refund under way sends the money back; a fresh quote would ask the payer to top up what is leaving (the
        # page hides "Refresh price" then, UX-019; this holds for any caller, 2026-10-12).
        snapshot = await self.load_snapshot(invoice)
        if any(
            operation.status.is_active
            for operation in list_refunds_in_asset(snapshot.operations, asset_symbol=invoice.asset_symbol)
        ):
            raise InvoiceStateError("a refund for this invoice is under way, so it takes no more payments")
        latest_quote = await self.price_quote_service.find_latest_quote(invoice.id)
        now = datetime.now(UTC)
        if latest_quote is not None and latest_quote.is_live_at(now):
            return await self.read_public_invoice(invoice_id)
        asset = self.chains_service.get_asset(invoice.chain_key, symbol=invoice.asset_symbol)
        quote = await self.price_quote_service.create_quote(
            invoice.id,
            chain_key=invoice.chain_key,
            asset=asset,
            fiat_currency=invoice.fiat_currency,
            fiat_amount=invoice.fiat_amount - invoice.credited_fiat_amount,
            lifetime_seconds=invoice.lifetime_seconds,
        )
        invoice.amount_due_base_units = invoice.received_base_units + quote.amount_base_units
        await self.repository.session.flush()
        return await self.read_public_invoice(invoice_id)

    # Refunds (PLAN.md section 6)

    async def create_refund_offers(self, invoice_id: str) -> RefundOfferListResponse:
        """Not screened: only the address that paid can sign for the money, and screening a payer at refund time
        could refuse the payer who paid (2026-10-09)."""
        invoice = await self.get_invoice(invoice_id, lock=True)
        snapshot = await self.load_snapshot(invoice)
        asset = snapshot.asset
        if asset is None or invoice.status not in {InvoiceStatus.OVERPAID, InvoiceStatus.UNDERPAID}:
            raise RefundNotAllowedError("refunds are offered only for overpaid or underpaid invoices")
        if not snapshot.float_refunds_allowed:
            raise RefundNotAllowedError("this invoice pays its receiver directly; settle any refund with them")
        if is_underpaid_within_payment_window(invoice, now=datetime.now(UTC)):
            raise RefundNotAllowedError("an underpaid amount can be asked back once the payment window has passed")
        if any(
            operation.status.is_active
            for operation in list_refunds_in_asset(snapshot.operations, asset_symbol=invoice.asset_symbol)
        ):
            raise RefundNotAllowedError("a refund for this invoice is already under way")
        offerable_senders = [
            sender
            for sender in compute_refundable_split(snapshot).senders
            if sender.refundable_base_units > 0 and not sender.sender_has_code
        ]
        if offerable_senders and not has_usd_price(asset, snapshot=snapshot):
            # Without a price the minimum is unknown, not missed (presenters.present_refund_offer, 2026-10-13).
            raise PriceUnavailableError("prices are unavailable right now; try again shortly")
        drafts = [
            RefundOfferDraft(sender_address=sender.sender, amount_base_units=sender.refundable_base_units)
            for sender in offerable_senders
            if is_at_least_minimum(sender.refundable_base_units, snapshot=snapshot)
        ]
        if not drafts:
            raise RefundNotAllowedError("nothing on this invoice can be refunded automatically")
        offers = await self.settlement_service.create_refund_offers(
            invoice.id,
            public_base_url=self.wiring.public_base_url,
            chain=self.chains_service.get_evm_chain(snapshot.chain.key),
            deposit_address=require_deposit_address(invoice),
            asset=asset,
            drafts=drafts,
        )
        return RefundOfferListResponse(
            offers=[
                RefundOfferItemResponse(
                    sender=offer.sender_address,
                    amount=format_base_units(offer.amount_base_units, decimals=asset.decimals),
                    asset=offer.asset_symbol,
                    nonce=offer.nonce,
                    message=offer.message,
                    expires_at=offer.expires_at,
                )
                for offer in offers
            ]
        )

    async def refund_overpaid_excess_automatically(self, invoice: Invoice) -> set[str]:
        """Each plain-account sender's final excess, at least the minimum refund, goes back to it from the float
        automatically once final, no signed request needed. Called by the watcher on the locked invoice.
        A sender that had any refund on this invoice gets no automatic second one: a failed or owner-unpaid refund is
        asked for again with a signature (or settled by the owner), so a refund that reverts cannot loop on gas.
        Only an invoice paying the operator's own address is refunded: any other receiver keeps the excess (its sweep
        already took it) and settles with its payer (2026-10-09). Returns the asset symbols whose decision waits for
        a price (``ChainReadingsService.defer_automatic_refunds``)."""
        if (
            invoice.status != InvoiceStatus.OVERPAID
            or invoice.asset_symbol is None
            or not self.allows_float_refunds(invoice)
        ):
            return set()
        snapshot = await self.load_snapshot(invoice)
        asset = snapshot.asset
        if asset is None:
            return set()
        senders_refunded_before = {
            operation.recipient_address
            for operation in list_refunds_in_asset(snapshot.operations, asset_symbol=invoice.asset_symbol)
        }
        for sender in compute_refundable_split(snapshot).senders:
            # A mint's 0x0 sender is the owner's to settle; rows stored before 2026-10-13 say it has no code.
            if (
                sender.refundable_base_units <= 0
                or sender.sender_has_code
                or sender.sender == ZERO_ADDRESS
                or sender.sender in senders_refunded_before
            ):
                continue
            if not has_usd_price(asset, snapshot=snapshot):
                return {asset.symbol}
            if not is_at_least_minimum(sender.refundable_base_units, snapshot=snapshot):
                continue
            operation = await self.settlement_service.record_refund(
                invoice.id,
                chain_key=invoice.chain_key,
                asset=asset,
                recipient_address=sender.sender,
                amount_base_units=sender.refundable_base_units,
            )
            log.emit(logger, event=OverpayRefundRecorded(invoice_id=invoice.id, operation_id=operation.operation_id))
        return set()

    async def refund_stray_payments_automatically(self, invoice: Invoice) -> set[str]:
        """Money in an asset the invoice did not ask for goes back by itself once final, under the overpay excess's
        rules (after INC-001): only on an invoice paying the operator, only to a
        plain-account sender, at most what it sent in that asset less what went or is going back, at least the
        minimum refund. Called on the locked invoice whenever a stray row goes final. A sender with any refund in that
        asset not yet final (under way, failed or with the owner) gets no new one, so a refund that reverts cannot
        loop on gas; contract and unknown senders are the owner's (their event says so). Returns the asset symbols
        whose decision waits for a price (``ChainReadingsService.defer_automatic_refunds``)."""
        if not self.allows_float_refunds(invoice):
            return set()
        snapshot = await self.load_snapshot(invoice)
        deferred_asset_symbols: set[str] = set()
        for stray_refundable in compute_stray_refundables(snapshot):
            sender = stray_refundable.sender
            # A mint's 0x0 sender is the owner's to settle; rows stored before 2026-10-13 say it has no code.
            if (
                sender is None
                or sender == ZERO_ADDRESS
                or stray_refundable.sender_has_code
                or stray_refundable.refundable_base_units <= 0
            ):
                continue
            refunds_not_final = [
                operation
                for operation in list_refunds_in_asset(snapshot.operations, asset_symbol=stray_refundable.asset.symbol)
                if operation.recipient_address == sender and operation.status != OperationStatus.FINAL
            ]
            if refunds_not_final:
                continue
            if not has_usd_price(stray_refundable.asset, snapshot=snapshot):
                deferred_asset_symbols.add(stray_refundable.asset.symbol)
                continue
            if not is_asset_amount_at_least_minimum(
                stray_refundable.refundable_base_units, asset=stray_refundable.asset, snapshot=snapshot
            ):
                continue
            operation = await self.settlement_service.record_refund(
                invoice.id,
                chain_key=invoice.chain_key,
                asset=stray_refundable.asset,
                recipient_address=sender,
                amount_base_units=stray_refundable.refundable_base_units,
            )
            log.emit(
                logger,
                event=StrayRefundRecorded(
                    invoice_id=invoice.id,
                    operation_id=operation.operation_id,
                    asset_symbol=stray_refundable.asset.symbol,
                ),
            )
        return deferred_asset_symbols

    async def request_signed_refund(self, invoice_id: str, *, nonce: str, signature: str) -> InvoicePublicResponse:
        """Recover the signer, consume the nonce, recheck the cap and record the refund, all in this transaction (not
        screened, as ``create_refund_offers``)."""
        invoice = await self.get_invoice(invoice_id, lock=True)
        offer = await self.settlement_service.consume_signed_refund_offer(invoice.id, nonce=nonce, signature=signature)
        await self.record_refund(invoice, sender=offer.sender_address, amount_base_units=offer.amount_base_units)
        return await self.read_public_invoice(invoice_id)

    async def create_account_refund(
        self, invoice_id: str, *, account_id: int, sender: str, amount_text: str
    ) -> InvoiceAccountResponse:
        """The account's refund of its own invoice to a recorded sender (contract senders included), under the same
        caps and only on an invoice paying the operator (``record_refund``)."""
        invoice = await self.get_account_invoice(invoice_id, account_id=account_id, lock=True)
        if invoice.asset_symbol is None:
            raise RefundNotAllowedError("the invoice has no asset yet")
        asset = self.chains_service.get_asset(invoice.chain_key, symbol=invoice.asset_symbol)
        amount_base_units = to_base_units(
            parse_decimal_amount(amount_text, field_name="amount"), decimals=asset.decimals, field_name="amount"
        )
        await self.record_refund(invoice, sender=to_checksum_address(sender), amount_base_units=amount_base_units)
        return await self.read_account_invoice(invoice_id, account_id=account_id)

    async def record_refund(self, invoice: Invoice, *, sender: str, amount_base_units: int) -> None:
        """The one way a signed or account refund is recorded; refused unless the invoice pays the operator."""
        snapshot = await self.load_snapshot(invoice)
        asset = snapshot.asset
        if asset is None or invoice.status not in {InvoiceStatus.OVERPAID, InvoiceStatus.UNDERPAID}:
            raise RefundNotAllowedError("refunds are possible only for overpaid or underpaid invoices")
        if not snapshot.float_refunds_allowed:
            raise RefundNotAllowedError("this invoice pays its receiver directly; settle any refund with them")
        if sender == ZERO_ADDRESS:
            raise RefundNotAllowedError("the zero address is a token mint, not a sender; no refund can go there")
        refundable = next(
            (
                share.refundable_base_units
                for share in compute_refundable_split(snapshot).senders
                if share.sender == sender
            ),
            0,
        )
        if amount_base_units <= 0 or amount_base_units > refundable:
            available = format_base_units(refundable, decimals=asset.decimals)
            raise RefundNotAllowedError(f"at most {available} {asset.symbol} can be refunded to that sender")
        await self.settlement_service.record_refund(
            invoice.id,
            chain_key=invoice.chain_key,
            asset=asset,
            recipient_address=sender,
            amount_base_units=amount_base_units,
        )
        if invoice.status == InvoiceStatus.UNDERPAID and invoice.closed_at is None:
            # A refunded underpaid invoice takes no more top-ups; what is in the deposit goes to the destination.
            invoice.closed_at = datetime.now(UTC)
            await self.request_sweep_of_a_closing_deposit(invoice, asset=asset)
