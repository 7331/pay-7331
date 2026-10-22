"""InvoiceCreation: making an invoice (PLAN.md sections 3, 4, 14 and 15), split out of ``service.py``.

The salt is derived from the invoice id and ``PAY_INVOICE_SALT_SEED`` (deposit_salts.py). An invoice offering one chain
asks that chain's factory for its deposit at once (one ``eth_call``, ``evm_deposit_targets.py``); one offering several
EVM chains keeps only its checked destination and derives the deposit when the payer picks a (chain, asset) pair
(``invoice_asset_choice.py``): the destination is validated on every offered chain here, the factory is asked for the
chosen one only there. An XMR invoice is paid to a fresh subaddress of the account's Monero wallet instead
(``monero_invoice_creation.py``). A fiat invoice with its one coin on its one chain known gets a quote at once; any
other waits in ``awaiting_asset`` until the payer picks.
"""

from decimal import Decimal

from app.infrastructure.evm_rpc_api.client import (
    EvmRpcEndpoint,
    decode_uint256,
    encode_native_balance_call,
    encode_token_balance_call,
)
from app.infrastructure.evm_rpc_api.errors import EvmRpcUnavailableError
from app.infrastructure.evm_rpc_api.schemas import Multicall3Result
from app.payments.accounts.preferences_service import AccountPreferencesService
from app.payments.chains.errors import ChainKindNotSupportedError
from app.payments.chains.service import ChainsService
from app.payments.chains.types import (
    AssetConfiguration,
    ChainConfiguration,
    EvmChainConfiguration,
    MoneroChainConfiguration,
)
from app.payments.invoices.creation_limits import InvoiceCreationLimits
from app.payments.invoices.deposit_salts import derive_invoice_salt
from app.payments.invoices.enums import InvoiceCreationChannel, InvoiceStatus
from app.payments.invoices.errors import FactoryNotDeployedError, InvoiceNotRefreshableError
from app.payments.invoices.evm_deposit_targets import EvmDepositTargets
from app.payments.invoices.invoice_payment_terms import resolve_payment_terms_by_chain
from app.payments.invoices.log_events import InvoiceCreated, InvoiceRefreshed
from app.payments.invoices.models import Invoice
from app.payments.invoices.monero_invoice_creation import allocate_monero_payment_target
from app.payments.invoices.new_invoice_rows import InvoicePaymentTarget, build_new_invoice_row
from app.payments.invoices.offered_chains import FIAT_CURRENCIES, resolve_offered_assets
from app.payments.invoices.repository import InvoiceRepository
from app.payments.invoices.types import InvoicePaymentTerms, NewInvoice
from app.payments.invoices.wiring import InvoiceWiring
from app.payments.monero_wallets.service import MoneroWalletService
from app.payments.price_quotes.service import PriceQuoteService
from app.shared import logging as log
from app.shared.errors import InvalidInputError, ServiceUnavailableError
from app.shared.identifiers import generate_public_id

logger = log.get_logger(__name__)


# The smallest fiat invoice is 1 USD; a EUR one is at least 1 EUR.
MINIMUM_FIAT_INVOICE_AMOUNT = Decimal("1.00")


def refuse_below_minimum_sweep(amount_due_base_units: int, *, asset: AssetConfiguration) -> None:
    """An invoice for less than the asset's minimum sweep amount would cost more gas to sweep than it brings."""
    if amount_due_base_units < asset.minimum_sweep_base_units:
        raise InvalidInputError(
            f"amount: below the minimum of {format(asset.minimum_sweep_amount, 'f')} {asset.symbol} for this asset"
        )


def refuse_monero_chain_without_account(chains: list[ChainConfiguration], *, account_id: int | None) -> None:
    """An XMR invoice is paid to the account's Monero wallet, so a signed-out maker cannot offer a Monero chain."""
    for chain in chains:
        if isinstance(chain, MoneroChainConfiguration) and account_id is None:
            raise ChainKindNotSupportedError(
                f"{chain.name} invoices need an account with a Monero wallet: sign in and set one in settings"
            )


def refuse_invalid_fiat_amount(amount: Decimal, *, currency: str) -> None:
    """A fiat amount has at most 2 decimals and is at least ``MINIMUM_FIAT_INVOICE_AMOUNT``."""
    if amount != amount.quantize(Decimal("0.01")):
        # It was rounded silently before (1.005 USD became 1.00, 2026-10-09).
        raise InvalidInputError("amount: at most 2 decimals for a fiat currency")
    if amount < MINIMUM_FIAT_INVOICE_AMOUNT:
        raise InvalidInputError(f"amount: the smallest invoice is {MINIMUM_FIAT_INVOICE_AMOUNT} {currency}")


class InvoiceCreation:
    def __init__(
        self,
        *,
        repository: InvoiceRepository,
        wiring: InvoiceWiring,
        price_quote_service: PriceQuoteService,
        monero_wallet_service: MoneroWalletService,
        creation_limits: InvoiceCreationLimits,
        evm_deposit_targets: EvmDepositTargets,
        account_preferences_service: AccountPreferencesService,
    ) -> None:
        self.repository = repository
        self.wiring = wiring
        self.chains_service: ChainsService = wiring.chains_service
        self.price_quote_service = price_quote_service
        self.monero_wallet_service = monero_wallet_service
        self.creation_limits = creation_limits
        self.evm_deposit_targets = evm_deposit_targets
        self.account_preferences_service = account_preferences_service

    async def create_invoice(
        self, new_invoice: NewInvoice, *, client_ip_address: str | None, creation_channel: InvoiceCreationChannel
    ) -> Invoice:
        """A new invoice, or the account's existing one when its ``order_id`` was used before (as Cryptomus), unless
        ``refresh_expired`` asks for a new one in place of an expired, unpaid one (``lock_refreshable_invoice``). The
        open-invoice caps are ``creation_limits.py``'s; an API token's creations are serialized per account,
        so a repeated ``order_id`` in a burst finds the first invoice (2026-10-12). An XMR invoice is an account's only,
        paid to a fresh subaddress of its Monero wallet (``monero_invoice_creation.py``; PLAN.md section 14)."""
        chains = [self.chains_service.get_chain(chain_key) for chain_key in dict.fromkeys(new_invoice.chain_keys)]
        refuse_monero_chain_without_account(chains, account_id=new_invoice.account_id)
        if creation_channel.is_capped_per_account:
            if new_invoice.account_id is None:
                raise InvalidInputError("an API token's or a schedule's invoice belongs to an account")
            await self.repository.lock_account_invoice_creation(new_invoice.account_id)
        order_invoice, refreshed_invoice = await self.find_order_invoice_or_refreshable_invoice(new_invoice)
        if order_invoice is not None:
            return order_invoice
        if new_invoice.callback_url is not None:
            await self.check_callback_url(new_invoice.callback_url, account_id=new_invoice.account_id)
        admitted_client_network = await self.creation_limits.admit_invoice_creation(
            new_invoice.account_id, client_ip_address=client_ip_address, creation_channel=creation_channel
        )
        offered = resolve_offered_assets(
            self.chains_service,
            chain_keys=[chain.key for chain in chains],
            asset_symbols=new_invoice.assets,
            assets_by_chain=new_invoice.assets_by_chain,
            defaulted_chains=new_invoice.defaulted_chains,
        )
        await self.creation_limits.admit_public_invoice_on_chains(
            new_invoice, chain_keys=list(offered), creation_channel=creation_channel
        )
        is_fiat = new_invoice.currency in FIAT_CURRENCIES
        await self.check_invoice_amount_against_offer(new_invoice, offered=offered, is_fiat=is_fiat)
        invoice_id = generate_public_id("inv_")
        invoice_salt = derive_invoice_salt(invoice_id, invoice_salt_seed=self.wiring.invoice_settings.invoice_salt_seed)
        payment_target, offered = await self.find_payment_target(
            new_invoice, offered=offered, invoice_salt=invoice_salt
        )
        # A chain left out of the defaults above may have been the first one.
        first_asset = next(iter(offered.values()))[0]
        invoice = build_new_invoice_row(
            new_invoice,
            invoice_id=invoice_id,
            offered_assets=offered,
            is_fiat=is_fiat,
            invoice_salt=invoice_salt,
            payment_target=payment_target,
            admitted_client_network=admitted_client_network,
            payment_terms_by_chain=await self.find_payment_terms_by_chain(new_invoice, chain_keys=list(offered)),
        )
        if not is_fiat:
            refuse_below_minimum_sweep(invoice.amount_due_base_units or 0, asset=first_asset)
        if refreshed_invoice is not None:
            # The old row first: the order's one current invoice moves to the new row in this transaction.
            refreshed_invoice.refreshed_into_invoice_id = invoice.id
            await self.repository.session.flush()
            invoice.refreshed_from_invoice_id = refreshed_invoice.id
        await self.repository.insert_invoice(invoice)
        if invoice.asset_symbol is not None and is_fiat:
            await self.quote_whole_amount(invoice, asset=first_asset)
        log.emit(logger, event=self.build_invoice_created_event(invoice, payment_target=payment_target))
        if refreshed_invoice is not None:
            log.emit(
                logger,
                event=InvoiceRefreshed(invoice_id=invoice.id, refreshed_from_invoice_id=refreshed_invoice.id),
            )
        return invoice

    async def find_order_invoice_or_refreshable_invoice(
        self, new_invoice: NewInvoice
    ) -> tuple[Invoice | None, Invoice | None]:
        """For an account's repeated ``order_id``: ``(the order's invoice to return, None)``, or ``(None, the order's
        expired invoice, locked)`` when ``refresh_expired`` makes a new one in its place; ``(None, None)`` otherwise."""
        if new_invoice.order_id is None or new_invoice.account_id is None:
            return None, None
        existing = await self.repository.find_account_invoice_by_order_id(
            new_invoice.order_id, account_id=new_invoice.account_id
        )
        if existing is None:
            return None, None
        if not new_invoice.refresh_expired or existing.status is not InvoiceStatus.EXPIRED:
            return existing, None
        refreshed_invoice = await self.lock_refreshable_invoice(existing)
        if refreshed_invoice.refreshed_into_invoice_id is not None or (
            refreshed_invoice.status is not InvoiceStatus.EXPIRED
        ):
            # Another request refreshed it first: its new invoice is the order's now.
            current = await self.repository.find_account_invoice_by_order_id(
                new_invoice.order_id, account_id=new_invoice.account_id
            )
            return current or refreshed_invoice, None
        return None, refreshed_invoice

    async def check_invoice_amount_against_offer(
        self, new_invoice: NewInvoice, *, offered: dict[str, list[AssetConfiguration]], is_fiat: bool
    ) -> None:
        """Refuses a crypto amount not in the one coin offered, an offered EVM chain with no deposit factory, a fiat
        amount with more than 2 decimals or below the minimum, and an XMR amount worth less than the minimum."""
        first_chain = self.chains_service.get_chain(next(iter(offered)))
        first_asset = offered[first_chain.key][0]
        if not is_fiat and (
            len(offered) != 1
            or len(offered[first_chain.key]) != 1
            or first_asset.symbol.casefold() != new_invoice.currency.casefold()
        ):
            raise InvalidInputError("currency: a crypto amount needs exactly one chain and that one asset")
        for chain_key in offered:
            if isinstance(self.chains_service.get_chain(chain_key), EvmChainConfiguration) and (
                chain_key not in self.wiring.factory_clients
            ):
                raise FactoryNotDeployedError(f"no deposit factory is configured on {chain_key} yet")
        if is_fiat:
            refuse_invalid_fiat_amount(new_invoice.amount, currency=new_invoice.currency)
        if not is_fiat and isinstance(first_chain, MoneroChainConfiguration):
            # Nothing is swept on this kind, so no minimum sweep guards it: the 1 USD invoice minimum does, at the
            # live price, before a subaddress minor is taken.
            await self.refuse_crypto_amount_below_fiat_minimum(
                new_invoice.amount, chain_key=first_chain.key, asset=first_asset
            )

    async def lock_refreshable_invoice(self, expired_invoice: Invoice) -> Invoice:
        """The order's expired invoice, locked (``FOR UPDATE``, as every invoice write) until the new one is in, so two
        refreshes make one new invoice and the watcher's late-money pass waits. Refused when money ever reached it:
        a payment, late money or stray money, final or not, or a mined balance on its deposit the watcher has not
        recorded yet (409 ``invoice_not_refreshable``). A payment still in the mempool cannot be seen; it lands as late
        money on the old invoice, and the payer has then paid twice. Nothing about the old invoice changes but its
        pointer to the new one; its deposit stays watched, as on any closed invoice (PLAN.md section 4)."""
        locked = await self.repository.find_invoice(expired_invoice.id, lock=True)
        if locked is None:
            raise RuntimeError("an invoice found by its order id is gone")
        if locked.refreshed_into_invoice_id is not None or locked.status is not InvoiceStatus.EXPIRED:
            return locked
        if (
            locked.late_payment
            or locked.received_base_units
            or locked.latest_gross_base_units
            or await self.repository.invoice_has_money_rows(locked.id)
            or await self.deposit_holds_sweepable_balance(locked)
        ):
            raise InvoiceNotRefreshableError(
                "refresh_expired: money reached this order's expired invoice; settle that invoice first"
            )
        return locked

    async def deposit_holds_sweepable_balance(self, invoice: Invoice) -> bool:
        """Whether the invoice's EVM deposit holds, at ``latest``, at least the minimum sweep amount of any coin its
        chain is configured with: one Multicall3 read, so money mined since the watcher's last pass still blocks a
        refresh. A failed balance call (a token without code on a fork) reads as nothing held. A Monero invoice, or an
        EVM one whose payer never picked a coin, has no deposit to read here."""
        chain = self.chains_service.get_chain(invoice.chain_key)
        if invoice.deposit_address is None or not isinstance(chain, EvmChainConfiguration):
            return False
        deposit_address = invoice.deposit_address
        balance_calls = [
            encode_native_balance_call(deposit_address)
            if asset.token_address is None
            else encode_token_balance_call(asset.token_address, holder_address=deposit_address)
            for asset in chain.assets
        ]

        async def read_latest_balances(endpoint: EvmRpcEndpoint) -> list[Multicall3Result]:
            return await endpoint.aggregate3(balance_calls, block_hash=None)

        try:
            results = await self.wiring.rpc_clients[chain.key].call_with_failover(read_latest_balances)
        except EvmRpcUnavailableError as exc:
            raise ServiceUnavailableError("the chain could not be reached; try again shortly") from exc
        return any(
            result.succeeded and decode_uint256(result.return_data) >= asset.minimum_sweep_base_units
            for asset, result in zip(chain.assets, results, strict=True)
        )

    async def check_callback_url(self, callback_url: str, *, account_id: int | None) -> None:
        """An account invoice's ``callback_url`` passes the account webhook's URL rules and needs the account's webhook
        secret to sign with (``InvoiceCallbackUrlCheck``)."""
        callback_url_check = self.wiring.invoice_callback_url_check
        if account_id is None or callback_url_check is None:
            raise InvalidInputError("callback_url: only an account's invoice takes one")
        await callback_url_check(self.repository.session, account_id, callback_url)

    async def find_payment_target(
        self,
        new_invoice: NewInvoice,
        *,
        offered: dict[str, list[AssetConfiguration]],
        invoice_salt: bytes,
    ) -> tuple[InvoicePaymentTarget, dict[str, list[AssetConfiguration]]]:
        """Where the invoice is paid, and the offered chains that remain. Monero: a fresh subaddress. EVM: the
        destination checked on every offered chain (a refusal on any stands; PLAN.md section 3), then the deposit the
        factory predicts when one chain remains, or none yet when the payer still picks the chain."""
        first_chain = self.chains_service.get_chain(next(iter(offered)))
        if isinstance(first_chain, MoneroChainConfiguration):
            if new_invoice.account_id is None:
                raise ChainKindNotSupportedError("a Monero invoice needs an account with a Monero wallet")
            # The wallet is the destination: an EVM `destination_address` (the payout address default) is not used.
            payment_target = await allocate_monero_payment_target(
                new_invoice.account_id,
                chain_key=first_chain.key,
                confirmations_final=await self.find_confirmations_final(new_invoice.account_id, chain=first_chain),
                repository=self.repository,
                monero_wallet_service=self.monero_wallet_service,
            )
            return payment_target, offered
        destination_address, passed_chains = await self.evm_deposit_targets.check_destination_on_offered_chains(
            new_invoice.destination_address,
            chains=[self.chains_service.get_evm_chain(chain_key) for chain_key in offered],
            allow_contract_destination=new_invoice.allow_contract_destination,
            defaulted_chains=new_invoice.defaulted_chains,
        )
        passed_chain_keys = {chain.key for chain in passed_chains}
        offered = {chain_key: assets for chain_key, assets in offered.items() if chain_key in passed_chain_keys}
        if len(offered) == 1:
            predicted = await self.evm_deposit_targets.predict_deposit(
                destination_address, chain=passed_chains[0], invoice_salt=invoice_salt
            )
            return (
                InvoicePaymentTarget(
                    deposit_address=predicted.deposit_address,
                    destination_address=predicted.destination_address,
                    latest_block_number=predicted.latest_block_number,
                ),
                offered,
            )
        return (
            InvoicePaymentTarget(
                deposit_address=None, destination_address=destination_address, latest_block_number=None
            ),
            offered,
        )

    async def find_payment_terms_by_chain(
        self, new_invoice: NewInvoice, *, chain_keys: list[str]
    ) -> dict[str, InvoicePaymentTerms]:
        """Each offered chain's payment window and shortfall (``invoice_payment_terms.py``): the account's saved
        defaults are read only when the maker left a term out, and never for a signed-out invoice."""
        saved_lifetime_seconds_by_chain: dict[str, int] = {}
        saved_accuracy_percent_by_chain: dict[str, Decimal] = {}
        if new_invoice.account_id is not None and (
            new_invoice.lifetime_seconds is None or new_invoice.accuracy_percent is None
        ):
            saved_payment_terms = await self.account_preferences_service.find_saved_invoice_payment_terms_by_chain(
                new_invoice.account_id
            )
            saved_lifetime_seconds_by_chain = saved_payment_terms.lifetime_seconds_by_chain
            saved_accuracy_percent_by_chain = saved_payment_terms.accuracy_percent_by_chain
        return resolve_payment_terms_by_chain(
            new_invoice,
            chain_keys=chain_keys,
            saved_lifetime_seconds_by_chain=saved_lifetime_seconds_by_chain,
            saved_accuracy_percent_by_chain=saved_accuracy_percent_by_chain,
        )

    async def find_confirmations_final(self, account_id: int, *, chain: MoneroChainConfiguration) -> int:
        """The confirmations a new XMR invoice of this account on this chain counts final at: the receiver's saved
        number, else the chain's ``chains.toml`` default. The invoice keeps whichever it was made
        with."""
        saved_by_chain = await self.account_preferences_service.find_saved_confirmations_final_by_chain(account_id)
        return saved_by_chain.get(chain.key) or chain.confirmations_final

    def build_invoice_created_event(self, invoice: Invoice, *, payment_target: InvoicePaymentTarget) -> InvoiceCreated:
        """On Monero ids, not addresses: a subaddress ties a payer to a receiver's wallet, and nothing recovers from
        it. On EVM the destination, and the deposit once it is derived (``InvoiceDepositDerived`` names it at the
        payer's pick otherwise)."""
        if isinstance(self.chains_service.get_chain(invoice.chain_key), MoneroChainConfiguration):
            return InvoiceCreated(
                invoice_id=invoice.id,
                chain_key=invoice.chain_key,
                monero_wallet_id=payment_target.monero_wallet_id,
                subaddress_minor=payment_target.subaddress_minor,
            )
        return InvoiceCreated(
            invoice_id=invoice.id,
            chain_key=invoice.chain_key,
            deposit_address=payment_target.deposit_address,
            destination_address=payment_target.destination_address,
        )

    async def refuse_crypto_amount_below_fiat_minimum(
        self, amount: Decimal, *, chain_key: str, asset: AssetConfiguration
    ) -> None:
        """Refuses a crypto amount worth less than ``MINIMUM_FIAT_INVOICE_AMOUNT`` USD at the live feed price (the
        same refusal a USD amount meets); 503 ``price_unavailable`` when no feed answers."""
        prices = await self.price_quote_service.read_chain_prices(chain_key)
        if amount * prices.asset_prices_usd[asset.symbol] < MINIMUM_FIAT_INVOICE_AMOUNT:
            raise InvalidInputError(
                f"amount: the smallest invoice is worth {MINIMUM_FIAT_INVOICE_AMOUNT} USD; "
                f"{format(amount.normalize(), 'f')} {asset.symbol} is worth less"
            )

    async def quote_whole_amount(self, invoice: Invoice, *, asset: AssetConfiguration) -> None:
        """The invoice's whole fiat amount quoted in ``asset`` on the invoice's chain; refused (503 ``price_unavailable``
        for a stale feed or a down sequencer) before anything is shown."""
        if invoice.fiat_currency is None or invoice.fiat_amount is None:
            return
        quote = await self.price_quote_service.create_quote(
            invoice.id,
            chain_key=invoice.chain_key,
            asset=asset,
            fiat_currency=invoice.fiat_currency,
            fiat_amount=invoice.fiat_amount,
            lifetime_seconds=invoice.lifetime_seconds,
        )
        refuse_below_minimum_sweep(quote.amount_base_units, asset=asset)
        invoice.amount_due_base_units = quote.amount_base_units
