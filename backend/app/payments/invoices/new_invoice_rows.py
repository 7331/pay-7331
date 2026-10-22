"""The row a new invoice starts as, on either chain kind (``InvoiceService.create_invoice``)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from app.payments.chains.types import AssetConfiguration
from app.payments.invoices.creation_limits import AdmittedClientNetwork
from app.payments.invoices.enums import InvoiceStatus
from app.payments.invoices.invoice_payment_terms import (
    find_payment_terms_while_choosing,
    present_payment_terms_by_chain,
)
from app.payments.invoices.models import Invoice
from app.payments.invoices.types import InvoicePaymentTerms, NewInvoice
from app.shared.amounts import to_base_units


class InvoicePaymentTarget(BaseModel):
    """Where a new invoice is paid: an EVM deposit clone and its destination, or a Monero subaddress and its wallet's
    primary address with the wallet and minor it was derived from. An invoice offering several chains has only its
    destination until the payer picks (``deposit_address`` None; PLAN.md section 15)."""

    model_config = ConfigDict(frozen=True)

    deposit_address: str | None
    destination_address: str
    # The `latest` block when the deposit was predicted (EVM); None on Monero until the watcher reads the tip, and
    # while the deposit waits for the pick.
    latest_block_number: int | None
    monero_wallet_id: int | None = None
    subaddress_minor: int | None = None
    # The confirmations this Monero invoice counts final at (the receiver's setting or the chain default); None on
    # EVM.
    confirmations_final: int | None = None


def build_new_invoice_row(
    new_invoice: NewInvoice,
    *,
    invoice_id: str,
    offered_assets: dict[str, list[AssetConfiguration]],
    is_fiat: bool,
    invoice_salt: bytes,
    payment_target: InvoicePaymentTarget,
    admitted_client_network: AdmittedClientNetwork | None,
    payment_terms_by_chain: dict[str, InvoicePaymentTerms],
) -> Invoice:
    """One chain with one asset starts ``awaiting_payment``; several assets or several chains ``awaiting_asset``. A
    crypto amount is due as asked. ``chain_key`` and ``assets`` are the first offered chain's: the placeholder an old
    client sees until the payer picks. ``payment_terms_by_chain`` has every offered chain's resolved terms
    (``invoice_payment_terms.py``)."""
    now = datetime.now(UTC)
    payment_terms = find_payment_terms_while_choosing(payment_terms_by_chain)
    chain_key, assets = next(iter(offered_assets.items()))
    single_asset = assets[0] if len(offered_assets) == 1 and len(assets) == 1 else None
    crypto_amount_base_units = (
        None if is_fiat else to_base_units(new_invoice.amount, decimals=assets[0].decimals, field_name="amount")
    )
    return Invoice(
        id=invoice_id,
        chain_key=chain_key,
        status=InvoiceStatus.AWAITING_PAYMENT if single_asset else InvoiceStatus.AWAITING_ASSET,
        partial=False,
        late_payment=False,
        sweep_blocked=False,
        assets=[asset.symbol for asset in assets],
        offered_assets_by_chain={
            offered_chain_key: [asset.symbol for asset in offered_chain_assets]
            for offered_chain_key, offered_chain_assets in offered_assets.items()
        },
        asset_symbol=single_asset.symbol if single_asset else None,
        invoice_salt=invoice_salt,
        deposit_address=payment_target.deposit_address,
        destination_address=payment_target.destination_address,
        monero_wallet_id=payment_target.monero_wallet_id,
        subaddress_minor=payment_target.subaddress_minor,
        confirmations_final=payment_target.confirmations_final,
        fiat_currency=new_invoice.currency if is_fiat else None,
        fiat_amount=new_invoice.amount.quantize(Decimal("0.01")) if is_fiat else None,
        crypto_amount_base_units=crypto_amount_base_units,
        amount_due_base_units=crypto_amount_base_units,
        received_base_units=0,
        credited_fiat_amount=Decimal(0),
        excess_base_units=0,
        latest_gross_base_units=0,
        latest_balance_base_units=0,
        latest_reading_block_number=payment_target.latest_block_number,
        note=new_invoice.note,
        account_id=new_invoice.account_id,
        client_ip_hash=admitted_client_network.client_ip_hash if admitted_client_network else None,
        client_ipv6_48_hash=admitted_client_network.client_ipv6_48_hash if admitted_client_network else None,
        order_id=new_invoice.order_id if new_invoice.account_id is not None else None,
        private_metadata=new_invoice.metadata,
        payment_link_id=new_invoice.payment_link_id,
        invoice_schedule_id=new_invoice.invoice_schedule_id,
        invoice_schedule_period_start=new_invoice.invoice_schedule_period_start,
        return_url=new_invoice.return_url,
        success_url=new_invoice.success_url,
        callback_url=new_invoice.callback_url if new_invoice.account_id is not None else None,
        lifetime_seconds=payment_terms.lifetime_seconds,
        accuracy_percent=payment_terms.accuracy_percent,
        payment_terms_by_chain=present_payment_terms_by_chain(payment_terms_by_chain),
        expires_at=now + timedelta(seconds=payment_terms.lifetime_seconds),
        underpaid_open_until=new_invoice.underpaid_open_until,
    )
