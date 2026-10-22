"""InvoiceAssetChoice: the payer's one tap on a (chain, asset) pair (PLAN.md sections 4 and 15), split out of
``service.py``.

Under the invoice row lock the pair is checked against what the invoice offered at creation (``offered_assets_by_chain``,
never changed afterwards), the coin is quoted on the chosen chain (a stale feed or a down sequencer refuses here, before
anything is shown), and, for an invoice that offered several chains, the chosen chain's factory predicts the deposit for
the invoice's own salt and its destination, which was fixed and checked at creation. The invoice takes the chosen
chain's payment window and accepted shortfall as resolved at creation (``invoice_payment_terms.py``), and the window
restarts from the pick. A pick is never changed: a second tap finds the invoice no longer awaiting a coin.
"""

from datetime import UTC, datetime, timedelta

from app.payments.chains.service import ChainsService
from app.payments.chains.types import EvmChainConfiguration
from app.payments.invoices.enums import InvoiceStatus
from app.payments.invoices.errors import FactoryNotDeployedError, InvoiceStateError
from app.payments.invoices.evm_deposit_targets import EvmDepositTargets
from app.payments.invoices.invoice_creation import InvoiceCreation
from app.payments.invoices.invoice_payment_terms import find_chosen_chain_payment_terms
from app.payments.invoices.log_events import InvoiceDepositDerived
from app.payments.invoices.models import Invoice, offered_assets_by_chain_of
from app.payments.invoices.wiring import InvoiceWiring
from app.shared import logging as log
from app.shared.errors import InvalidInputError

logger = log.get_logger(__name__)


def find_offered_pair(
    offered_assets_by_chain: dict[str, list[str]], *, chain_key: str | None, asset_symbol: str
) -> tuple[str, str]:
    """The one (chain key, asset symbol) the request names among the offered pairs. The coin matches
    case-insensitively; ``chain_key`` may be left out only when the coin is offered on one chain."""
    if chain_key is not None and chain_key not in offered_assets_by_chain:
        raise InvalidInputError("chain: not one offered here")
    candidates = [
        (offered_chain_key, offered_symbol)
        for offered_chain_key, offered_symbols in offered_assets_by_chain.items()
        if chain_key is None or offered_chain_key == chain_key
        for offered_symbol in offered_symbols
        if offered_symbol.casefold() == asset_symbol.casefold()
    ]
    if not candidates:
        raise InvalidInputError("asset: not one accepted here")
    if len(candidates) > 1:
        raise InvalidInputError("chain: this coin is offered on several networks; say which one")
    return candidates[0]


class InvoiceAssetChoice:
    def __init__(
        self,
        *,
        wiring: InvoiceWiring,
        invoice_creation: InvoiceCreation,
        evm_deposit_targets: EvmDepositTargets,
    ) -> None:
        self.wiring = wiring
        self.chains_service: ChainsService = wiring.chains_service
        self.invoice_creation = invoice_creation
        self.evm_deposit_targets = evm_deposit_targets

    def refuse_unless_awaiting_asset(self, invoice: Invoice, *, now: datetime) -> None:
        # An expired or cancelled one gets its own reason (2026-10-11: an expired picker was told the asset was set);
        # one past `expires_at` that the watcher has not closed yet counts as expired, so a pick cannot revive it.
        if invoice.status == InvoiceStatus.CANCELLED:
            raise InvoiceStateError("this invoice was cancelled; ask the seller for a new one")
        if invoice.status == InvoiceStatus.EXPIRED or (
            invoice.status == InvoiceStatus.AWAITING_ASSET
            and invoice.expires_at is not None
            and invoice.expires_at <= now
        ):
            raise InvoiceStateError("this invoice expired before a coin was picked; ask the seller for a new one")
        if invoice.status != InvoiceStatus.AWAITING_ASSET:
            raise InvoiceStateError("the coin for this invoice is already picked")

    async def choose_asset(self, invoice: Invoice, *, chain_key: str | None, asset_symbol: str) -> None:
        """Pick the (chain, asset) pair of the locked ``awaiting_asset`` invoice: starts its quote and its lifetime, and
        moves the invoice onto the chosen chain (``chain_key`` and ``assets`` were the first offered chain's)."""
        now = datetime.now(UTC)
        self.refuse_unless_awaiting_asset(invoice, now=now)
        offered_assets_by_chain = offered_assets_by_chain_of(invoice)
        chosen_chain_key, chosen_symbol = find_offered_pair(
            offered_assets_by_chain, chain_key=chain_key, asset_symbol=asset_symbol
        )
        asset = self.chains_service.get_asset(chosen_chain_key, symbol=chosen_symbol)
        chosen_chain_payment_terms = find_chosen_chain_payment_terms(invoice, chain_key=chosen_chain_key)
        invoice.lifetime_seconds = chosen_chain_payment_terms.lifetime_seconds
        invoice.accuracy_percent = chosen_chain_payment_terms.accuracy_percent
        invoice.chain_key = chosen_chain_key
        invoice.assets = list(offered_assets_by_chain[chosen_chain_key])
        invoice.asset_symbol = asset.symbol
        invoice.status = InvoiceStatus.AWAITING_PAYMENT
        invoice.expires_at = now + timedelta(seconds=invoice.lifetime_seconds)
        if invoice.deposit_address is None:
            await self.derive_deposit(invoice, chain=self.chains_service.get_evm_chain(chosen_chain_key))
        # Quote queries may autoflush the invoice: its chosen asset must already have its deposit for the database
        # constraint. Any quote refusal still rolls the whole selection back in the request transaction.
        await self.invoice_creation.quote_whole_amount(invoice, asset=asset)

    async def derive_deposit(self, invoice: Invoice, *, chain: EvmChainConfiguration) -> None:
        """The chosen chain's deposit clone for the invoice's unchanged salt and fixed destination; the watcher reads
        from the block it was predicted at."""
        if chain.key not in self.wiring.factory_clients:
            raise FactoryNotDeployedError(f"no deposit factory is configured on {chain.key} yet")
        predicted = await self.evm_deposit_targets.predict_deposit(
            invoice.destination_address, chain=chain, invoice_salt=invoice.invoice_salt
        )
        invoice.deposit_address = predicted.deposit_address
        invoice.latest_reading_block_number = predicted.latest_block_number
        log.emit(
            logger,
            event=InvoiceDepositDerived(
                invoice_id=invoice.id,
                chain_key=chain.key,
                deposit_address=predicted.deposit_address,
                destination_address=predicted.destination_address,
            ),
        )
