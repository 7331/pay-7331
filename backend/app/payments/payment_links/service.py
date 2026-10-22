"""PaymentLinkService: quantity links, on one session.

A payer's tap on a coin takes a slot: under a ``FOR UPDATE`` lock on the link row the slots held are counted and, while
one is free, a normal invoice is made through ``InvoiceService.create_invoice`` (the per-IP open-invoice cap and its
advisory lock included) in the same transaction. A second payer racing for the last slot waits on the row
lock and then counts the first one's committed invoice, so a link never hands out more slots than its quantity. Lock
order is link row, then the client network's advisory lock; nothing takes them the other way round.
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.monero_sidecar_api.errors import MoneroSidecarError, MoneroSidecarInputRefusedError
from app.payments.chains.service import present_chain
from app.payments.chains.types import EvmChainConfiguration, MoneroChainConfiguration
from app.payments.invoices.errors import FactoryNotDeployedError
from app.payments.invoices.invoice_asset_choice import find_offered_pair
from app.payments.invoices.invoice_creation import MINIMUM_FIAT_INVOICE_AMOUNT
from app.payments.invoices.offered_chains import read_requested_chain_keys, resolve_offered_assets
from app.payments.invoices.schemas import InvoicePublicResponse
from app.payments.invoices.service import InvoiceService
from app.payments.invoices.types import NewInvoice, PaymentLinkInvoiceCounts
from app.payments.monero_wallets.errors import MoneroSidecarUnavailableError, MoneroWalletMissingError
from app.payments.payment_links.enums import PaymentLinkStatus
from app.payments.payment_links.errors import (
    PaymentLinkClosedError,
    PaymentLinkNotFoundError,
    PaymentLinkSoldOutError,
)
from app.payments.payment_links.log_events import (
    PaymentLinkClosed,
    PaymentLinkCreated,
    PaymentLinkInvoiceCreated,
    PaymentLinkSoldOutRefused,
)
from app.payments.payment_links.models import PaymentLink, offered_assets_by_chain_of
from app.payments.payment_links.repository import PaymentLinkRepository
from app.payments.payment_links.schemas import (
    CreatePaymentLinkRequest,
    PaymentLinkAccountResponse,
    PaymentLinkDetailResponse,
    PaymentLinkListResponse,
    PaymentLinkPublicResponse,
)
from app.shared import logging as log
from app.shared.amounts import parse_decimal_amount
from app.shared.errors import InvalidInputError
from app.shared.identifiers import generate_public_id

logger = log.get_logger(__name__)

PAYMENT_LINKS_PER_ACCOUNT_PAGE = 100
INVOICES_PER_PAYMENT_LINK_PAGE = 200


def decide_payment_link_status(payment_link: PaymentLink, *, counts: PaymentLinkInvoiceCounts) -> PaymentLinkStatus:
    if payment_link.closed_at is not None or payment_link.account_id is None:
        return PaymentLinkStatus.CLOSED
    if counts.paid >= payment_link.quantity:
        return PaymentLinkStatus.SOLD_OUT
    if counts.slots_held >= payment_link.quantity:
        return PaymentLinkStatus.ALL_TAKEN
    return PaymentLinkStatus.OPEN


class PaymentLinkService:
    def __init__(self, *, repository: PaymentLinkRepository, invoice_service: InvoiceService) -> None:
        self.repository = repository
        self.invoice_service = invoice_service
        self.chains_service = invoice_service.chains_service

    @classmethod
    def on_session(cls, session: AsyncSession, *, invoice_service: InvoiceService) -> Self:
        return cls(repository=PaymentLinkRepository(session=session), invoice_service=invoice_service)

    # Presenting

    async def present_public(
        self, payment_link: PaymentLink, *, counts: PaymentLinkInvoiceCounts
    ) -> PaymentLinkPublicResponse:
        fiat, payment_options = await self.invoice_service.read_fiat_payment_options(
            payment_link.fiat_amount,
            fiat_currency=payment_link.fiat_currency,
            legacy_chain_key=payment_link.chain_key,
            offered_assets_by_chain=offered_assets_by_chain_of(payment_link),
        )
        status = decide_payment_link_status(payment_link, counts=counts)
        return PaymentLinkPublicResponse(
            id=payment_link.id,
            url=f"{self.invoice_service.wiring.public_base_url}/l/{payment_link.id}",
            status=status,
            quantity=payment_link.quantity,
            slots_left=0 if status == PaymentLinkStatus.CLOSED else max(payment_link.quantity - counts.slots_held, 0),
            chain=present_chain(self.chains_service.get_chain(payment_link.chain_key)),
            assets=payment_link.assets,
            payment_options=payment_options,
            fiat=fiat,
            note=payment_link.note,
            created_at=payment_link.created_at,
        )

    async def find_current_destination_address(self, payment_link: PaymentLink) -> str:
        """Where the link's next payer pays: on EVM the address it was made with; on Monero the account's active
        wallet, which each payer's invoice takes at the tap (a link is not an invoice: PLAN.md section 14). The
        address stored at creation stands only if the account has no active wallet there any more."""
        if payment_link.account_id is None or not isinstance(
            self.chains_service.get_chain(payment_link.chain_key), MoneroChainConfiguration
        ):
            return payment_link.destination_address
        monero_wallet = await self.invoice_service.monero_wallet_service.find_active_wallet(
            payment_link.account_id, chain_key=payment_link.chain_key
        )
        return monero_wallet.primary_address if monero_wallet is not None else payment_link.destination_address

    async def present_account(
        self, payment_link: PaymentLink, *, counts: PaymentLinkInvoiceCounts
    ) -> PaymentLinkAccountResponse:
        public_payment_link_response = await self.present_public(payment_link, counts=counts)
        return PaymentLinkAccountResponse(
            id=public_payment_link_response.id,
            url=public_payment_link_response.url,
            status=public_payment_link_response.status,
            quantity=public_payment_link_response.quantity,
            slots_left=public_payment_link_response.slots_left,
            chain=public_payment_link_response.chain,
            assets=public_payment_link_response.assets,
            payment_options=public_payment_link_response.payment_options,
            fiat=public_payment_link_response.fiat,
            note=public_payment_link_response.note,
            created_at=public_payment_link_response.created_at,
            destination_address=await self.find_current_destination_address(payment_link),
            lifetime_seconds=payment_link.lifetime_seconds,
            accuracy_percent=format(payment_link.accuracy_percent.normalize(), "f"),
            return_url=payment_link.return_url,
            slots_held=counts.slots_held,
            paid_count=counts.paid,
            closed_at=payment_link.closed_at,
        )

    async def count_invoices(self, payment_link: PaymentLink) -> PaymentLinkInvoiceCounts:
        return (await self.invoice_service.count_payment_link_invoices([payment_link.id]))[payment_link.id]

    async def get_payment_link(self, payment_link_id: str, *, lock: bool = False) -> PaymentLink:
        payment_link = await self.repository.find_payment_link(payment_link_id, lock=lock)
        if payment_link is None:
            raise PaymentLinkNotFoundError("No payment link with that id.")
        return payment_link

    async def get_account_payment_link(
        self, payment_link_id: str, *, account_id: int, lock: bool = False
    ) -> PaymentLink:
        """The account's own link; another's is not found, as if it did not exist."""
        payment_link = await self.get_payment_link(payment_link_id, lock=lock)
        if payment_link.account_id != account_id:
            raise PaymentLinkNotFoundError("No payment link with that id.")
        return payment_link

    # The account

    async def create_payment_link(
        self, link_request: CreatePaymentLinkRequest, *, account_id: int, payout_address: str
    ) -> tuple[PaymentLinkAccountResponse, dict[str, list[str]]]:
        """The view and the coins each offered chain accepts (the account's new starting ticks there). Checked as an
        invoice would be (fiat amount, coins, destination on every offered chain) so every payer's invoice can be made.
        On a
        Monero chain the account needs an active Monero wallet (``monero_wallet_missing`` without one) and the scanner
        sidecar must answer (503 ``monero_sidecar_unavailable`` otherwise, as an XMR invoice would be refused); each
        payer's tap takes the wallet active then, and the account view shows that one (PLAN.md section 14)."""
        offered = resolve_offered_assets(
            self.chains_service,
            chain_keys=read_requested_chain_keys(
                chain=link_request.chain, chains=link_request.chains, assets_by_chain=link_request.assets_by_chain
            ),
            asset_symbols=link_request.assets or [],
            assets_by_chain=link_request.assets_by_chain,
            defaulted_chains=False,
        )
        chain = self.chains_service.get_chain(next(iter(offered)))
        for offered_chain_key in offered:
            if (
                isinstance(self.chains_service.get_chain(offered_chain_key), EvmChainConfiguration)
                and offered_chain_key not in self.invoice_service.wiring.factory_clients
            ):
                raise FactoryNotDeployedError(f"no deposit factory is configured on {offered_chain_key} yet")
        currency = link_request.currency.value
        amount = parse_decimal_amount(link_request.amount, field_name="amount")
        if amount != amount.quantize(Decimal("0.01")):
            raise InvalidInputError("amount: at most 2 decimals for a fiat currency")
        if amount < MINIMUM_FIAT_INVOICE_AMOUNT:
            raise InvalidInputError(f"amount: the smallest invoice is {MINIMUM_FIAT_INVOICE_AMOUNT} {currency}")
        offered_symbols = {
            chain_key: [asset.symbol for asset in chain_assets] for chain_key, chain_assets in offered.items()
        }
        if isinstance(chain, EvmChainConfiguration):
            destination_address, _ = await self.invoice_service.evm_deposit_targets.check_destination_on_offered_chains(
                link_request.destination_address or payout_address,
                chains=[self.chains_service.get_evm_chain(offered_chain_key) for offered_chain_key in offered],
                allow_contract_destination=link_request.allow_contract_destination,
                defaulted_chains=False,
            )
        else:
            monero_wallet = await self.invoice_service.monero_wallet_service.find_active_wallet(
                account_id, chain_key=chain.key
            )
            if monero_wallet is None:
                raise MoneroWalletMissingError("monero_wallet: set a Monero wallet in settings first")
            try:
                await self.invoice_service.wiring.monero_sidecar.read_chain_health(chain.key)
            except (MoneroSidecarError, MoneroSidecarInputRefusedError) as sidecar_failure:
                raise MoneroSidecarUnavailableError(
                    "the Monero scanner is not available right now; try again later"
                ) from sidecar_failure
            destination_address = monero_wallet.primary_address
        payment_link = await self.repository.insert_payment_link(
            PaymentLink(
                id=generate_public_id("lnk_"),
                account_id=account_id,
                chain_key=chain.key,
                assets=offered_symbols[chain.key],
                offered_assets_by_chain=offered_symbols,
                fiat_currency=currency,
                fiat_amount=amount,
                destination_address=destination_address,
                note=link_request.note,
                return_url=link_request.return_url,
                quantity=link_request.quantity,
                lifetime_seconds=link_request.lifetime_seconds,
                accuracy_percent=link_request.accuracy_percent,
            )
        )
        log.emit(
            logger,
            event=PaymentLinkCreated(
                payment_link_id=payment_link.id, chain_key=chain.key, quantity=payment_link.quantity
            ),
        )
        return (
            await self.present_account(payment_link, counts=PaymentLinkInvoiceCounts(slots_held=0, paid=0)),
            offered_symbols,
        )

    async def list_account_payment_links(self, account_id: int) -> PaymentLinkListResponse:
        payment_links = await self.repository.list_account_payment_links(
            account_id, limit=PAYMENT_LINKS_PER_ACCOUNT_PAGE
        )
        counts_by_link = await self.invoice_service.count_payment_link_invoices([link.id for link in payment_links])
        return PaymentLinkListResponse(
            payment_links=[
                await self.present_account(payment_link, counts=counts_by_link[payment_link.id])
                for payment_link in payment_links
            ]
        )

    async def read_account_payment_link(self, payment_link_id: str, *, account_id: int) -> PaymentLinkDetailResponse:
        payment_link = await self.get_account_payment_link(payment_link_id, account_id=account_id)
        account_payment_link_response = await self.present_account(
            payment_link, counts=await self.count_invoices(payment_link)
        )
        return PaymentLinkDetailResponse(
            id=account_payment_link_response.id,
            url=account_payment_link_response.url,
            status=account_payment_link_response.status,
            quantity=account_payment_link_response.quantity,
            slots_left=account_payment_link_response.slots_left,
            chain=account_payment_link_response.chain,
            assets=account_payment_link_response.assets,
            payment_options=account_payment_link_response.payment_options,
            fiat=account_payment_link_response.fiat,
            note=account_payment_link_response.note,
            created_at=account_payment_link_response.created_at,
            destination_address=account_payment_link_response.destination_address,
            lifetime_seconds=account_payment_link_response.lifetime_seconds,
            accuracy_percent=account_payment_link_response.accuracy_percent,
            return_url=account_payment_link_response.return_url,
            slots_held=account_payment_link_response.slots_held,
            paid_count=account_payment_link_response.paid_count,
            closed_at=account_payment_link_response.closed_at,
            invoices=await self.invoice_service.list_payment_link_invoices(
                payment_link.id, account_id=account_id, limit=INVOICES_PER_PAYMENT_LINK_PAGE
            ),
        )

    async def close_payment_link(self, payment_link_id: str, *, account_id: int) -> PaymentLinkAccountResponse:
        """No new payer after this; invoices already made run their course."""
        payment_link = await self.get_account_payment_link(payment_link_id, account_id=account_id, lock=True)
        if payment_link.closed_at is None:
            payment_link.closed_at = datetime.now(UTC)
            await self.repository.session.flush()
            log.emit(logger, event=PaymentLinkClosed(payment_link_id=payment_link.id))
        return await self.present_account(payment_link, counts=await self.count_invoices(payment_link))

    # The payer

    async def read_public_payment_link(self, payment_link_id: str) -> PaymentLinkPublicResponse:
        payment_link = await self.get_payment_link(payment_link_id)
        return await self.present_public(payment_link, counts=await self.count_invoices(payment_link))

    async def create_payment_link_invoice(
        self, payment_link_id: str, *, chain_key: str | None, asset_symbol: str, client_ip_address: str
    ) -> InvoicePublicResponse:
        """Take a slot: the payer's own invoice in the picked coin on the picked chain, its quote live at once. 409 ``sold_out`` when every
        slot is paid or held by an open invoice, 409 ``payment_link_closed`` once closed."""
        payment_link = await self.get_payment_link(payment_link_id, lock=True)
        if payment_link.closed_at is not None or payment_link.account_id is None:
            raise PaymentLinkClosedError("this link is closed; ask the seller for a new one")
        # The pair is checked and the slot counted and taken under the link row lock, so a payer racing for the last
        # slot cannot oversell it whichever chain it picks.
        chosen_chain_key, chosen_symbol = find_offered_pair(
            offered_assets_by_chain_of(payment_link), chain_key=chain_key, asset_symbol=asset_symbol
        )
        # Counted under the row lock, after any earlier holder of it committed (READ COMMITTED reads afresh).
        counts = await self.count_invoices(payment_link)
        if counts.slots_held >= payment_link.quantity:
            log.emit(
                logger,
                event=PaymentLinkSoldOutRefused(payment_link_id=payment_link.id, quantity=payment_link.quantity),
            )
            raise PaymentLinkSoldOutError("sold out: every one of these is paid or being paid")
        invoice = await self.invoice_service.create_invoice(
            NewInvoice(
                chain_keys=[chosen_chain_key],
                amount=payment_link.fiat_amount,
                currency=payment_link.fiat_currency,
                assets=[chosen_symbol],
                destination_address=payment_link.destination_address,
                account_id=payment_link.account_id,
                # Checked when the link was made; skipping the code read keeps the row lock short.
                allow_contract_destination=True,
                note=payment_link.note,
                lifetime_seconds=payment_link.lifetime_seconds,
                accuracy_percent=payment_link.accuracy_percent,
                return_url=payment_link.return_url,
                payment_link_id=payment_link.id,
            ),
            client_ip_address=client_ip_address,
        )
        log.emit(
            logger,
            event=PaymentLinkInvoiceCreated(
                payment_link_id=payment_link.id,
                invoice_id=invoice.id,
                slots_held=counts.slots_held + 1,
                quantity=payment_link.quantity,
            ),
        )
        return await self.invoice_service.read_public_invoice(invoice.id)
