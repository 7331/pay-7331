"""The payer's "I paid" on an XMR invoice (PLAN.md sections 7.1 and 14).

Without a txid it rereads the invoice's ``monero_payments`` rows: an output credited but not final yet, or a pool
transaction (also one that just left the pool on its way into a block), is ``payment_confirming``; money already counted is ``payment_counted``; else ``nothing_found``. The
sidecar scans every block and the pool on its own, so there is nothing to fetch.

With a txid (64 hex, no ``0x``) the sidecar fetches the transaction, decodes its outputs with the invoice wallet's view
key (no tx key needed) and checks it on a second operator (``MoneroScannerReader.check_transaction``). The answer is
mapped onto the EVM outcomes and recorded nowhere: the sidecar writes the rows it finds and the watcher credits them,
so this check never credits an invoice. "Counted" is said only when this invoice counted an output of that txid at
final, at the confirmed tip the watcher stored (``latest_reading_block_number``), the same tip its final counting
read; money after the invoice closed is ``payment_after_close``.
"""

from datetime import UTC, datetime
import re

from app.infrastructure.monero_sidecar_api.errors import MoneroSidecarError, MoneroSidecarInputRefusedError
from app.infrastructure.monero_sidecar_api.schemas import MoneroTransactionCheck, MoneroTransactionCheckOutcome
from app.payments.chains.types import MoneroChainConfiguration
from app.payments.invoices.enums import PaymentCheckOutcome
from app.payments.invoices.models import Invoice, MoneroPayment
from app.payments.invoices.monero_payment_rules import (
    invoice_confirmations_final,
    is_counted_at_final,
    is_credited,
    is_on_its_way,
)
from app.payments.invoices.repository import InvoiceRepository
from app.payments.invoices.types import PaymentCheckResult
from app.payments.monero_wallets.errors import MoneroSidecarUnavailableError
from app.payments.monero_wallets.types import MoneroScannerReader

MONERO_TXID_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SIDECAR_OUTCOMES = {
    MoneroTransactionCheckOutcome.NOT_FOUND: PaymentCheckOutcome.TRANSACTION_UNKNOWN,
    MoneroTransactionCheckOutcome.FAILED: PaymentCheckOutcome.TRANSACTION_FAILED,
    MoneroTransactionCheckOutcome.OTHER_ADDRESS: PaymentCheckOutcome.OTHER_ADDRESS,
}


async def check_monero_invoice_payment(
    invoice: Invoice,
    *,
    chain: MoneroChainConfiguration,
    transaction_hash_text: str | None,
    repository: InvoiceRepository,
    scanner_reader: MoneroScannerReader,
) -> PaymentCheckResult:
    asset_symbol = chain.assets[0].symbol
    invoice_rows = await repository.list_monero_payments([invoice.id])
    if transaction_hash_text is None:
        return check_monero_invoice_rows_without_txid(invoice, invoice_rows=invoice_rows, asset_symbol=asset_symbol)
    txid = transaction_hash_text.strip().lower()
    if MONERO_TXID_PATTERN.fullmatch(txid) is None:
        return PaymentCheckResult(outcome=PaymentCheckOutcome.HASH_MALFORMED)
    transaction_check = await fetch_monero_transaction_check(
        invoice, chain=chain, txid=txid, scanner_reader=scanner_reader
    )
    if transaction_check.outcome in SIDECAR_OUTCOMES:
        return PaymentCheckResult(outcome=SIDECAR_OUTCOMES[transaction_check.outcome])
    outcome = decide_monero_transaction_outcome(invoice, chain=chain, txid=txid, invoice_rows=invoice_rows)
    return PaymentCheckResult(
        outcome=outcome, asset_symbol=asset_symbol, amount_base_units=transaction_check.amount_piconero
    )


def check_monero_invoice_rows_without_txid(
    invoice: Invoice, *, invoice_rows: list[MoneroPayment], asset_symbol: str
) -> PaymentCheckResult:
    """ "I paid" with no txid: credited but not counted yet, or on its way, is confirming; counted money is counted."""
    credited_amount = sum(row.amount_piconero for row in invoice_rows if is_credited(row))
    now = datetime.now(UTC)
    pool_amount = sum(row.amount_piconero for row in invoice_rows if is_on_its_way(row, now=now))
    if credited_amount > invoice.received_base_units or pool_amount > 0:
        return PaymentCheckResult(
            outcome=PaymentCheckOutcome.PAYMENT_CONFIRMING,
            asset_symbol=asset_symbol,
            amount_base_units=max(credited_amount - invoice.received_base_units, 0) + pool_amount,
        )
    if invoice.received_base_units > 0:
        return PaymentCheckResult(
            outcome=PaymentCheckOutcome.PAYMENT_COUNTED,
            asset_symbol=asset_symbol,
            amount_base_units=invoice.received_base_units,
        )
    return PaymentCheckResult(outcome=PaymentCheckOutcome.NOTHING_FOUND)


async def fetch_monero_transaction_check(
    invoice: Invoice, *, chain: MoneroChainConfiguration, txid: str, scanner_reader: MoneroScannerReader
) -> MoneroTransactionCheck:
    """The sidecar's check of one txid against the invoice wallet; a sidecar or node failure is unavailable."""
    try:
        transaction_check = await scanner_reader.check_transaction(chain.key, txid=txid, invoice_id=invoice.id)
    except (MoneroSidecarError, MoneroSidecarInputRefusedError) as sidecar_failure:
        raise MoneroSidecarUnavailableError(
            "the Monero scanner is not available right now; try again later"
        ) from sidecar_failure
    if transaction_check.outcome == MoneroTransactionCheckOutcome.UNAVAILABLE:
        raise MoneroSidecarUnavailableError("no Monero node answered; try again in a minute")
    return transaction_check


def decide_monero_transaction_outcome(
    invoice: Invoice, *, chain: MoneroChainConfiguration, txid: str, invoice_rows: list[MoneroPayment]
) -> PaymentCheckOutcome:
    """Counted when this invoice counted an output of the txid at final, after close when the closed invoice never saw
    it, else still confirming."""
    transaction_rows = [row for row in invoice_rows if row.txid == txid]
    if any(
        is_counted_at_final(
            row,
            tip_height=invoice.latest_reading_block_number,
            confirmations_final=invoice_confirmations_final(invoice, chain=chain),
        )
        for row in transaction_rows
    ):
        return PaymentCheckOutcome.PAYMENT_COUNTED
    if invoice.closed_at is not None and not transaction_rows:
        return PaymentCheckOutcome.PAYMENT_AFTER_CLOSE
    return PaymentCheckOutcome.PAYMENT_CONFIRMING
