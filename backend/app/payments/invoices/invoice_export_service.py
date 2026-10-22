"""InvoiceExportService: an account's invoices and their money movements as CSV, for accounting (2026-10-21).

One row per invoice (``row_kind`` ``invoice``), then one per payment, wrong-coin payment, sweep and refund of it, each
repeating the invoice's columns, so a sheet filtered to one kind still reads on its own. BTCPay's invoice report does
the same ("An invoice with several payments can produce several rows", https://docs.btcpayserver.org/Users/invoices/;
its columns in https://github.com/btcpayserver/btcpayserver/blob/master/BTCPayServer/Services/Reporting/
InvoicesReportProvider.cs: created date, id, currency, due, price, status, payment date, address, currency, amount
and rate); this keeps those and adds the chain's transaction hash, block and the sweep and refund rows.

Amounts are exact decimal strings in the row's coin, fiat amounts as stored (2 decimals), times ISO 8601 UTC. A
payment's ``asset_price_usd`` is the quote live at its block's time; blank when none was live then (a coin-amount
invoice, or a block after the quote lapsed). Streamed: invoices are read 200 at a time, oldest first, each batch in its
own short REPEATABLE READ read-only transaction, so a batch's invoices, payments, sweeps and refunds come from one
snapshot and a large account never sits in memory or holds one long transaction.

Text a person typed (note, order id) gets a leading ``'`` when it, or what follows its leading whitespace, starts
with ``=``, ``+``, ``-``, ``@``, a tab, CR or LF or the full-width forms of the first four (U+FF1D, U+FF0B, U+FF0D,
U+FF20); and a ``'`` goes after any ``;`` (and the whitespace after it) that one of those eight signs follows, so a
sheet that splits on ``;`` runs no formula either (OWASP's trigger list,
https://owasp.org/www-community/attacks/CSV_Injection). OWASP notes Excel may still run one after a save and reopen.
"""

from collections.abc import AsyncIterator
import csv
from datetime import UTC, datetime
import io
import re

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.payments.chains.types import AssetConfiguration, ChainConfiguration, MoneroChainConfiguration
from app.payments.invoices.enums import InvoiceStatus
from app.payments.invoices.models import (
    DepositSweepEvent,
    Invoice,
    InvoicePayment,
    InvoiceStrayPayment,
    MoneroPayment,
)
from app.payments.invoices.monero_payment_rules import invoice_confirmations_final, is_counted_at_final, is_credited
from app.payments.invoices.repository import InvoiceRepository
from app.payments.invoices.wiring import InvoiceWiring
from app.payments.price_quotes.service import PriceQuoteService
from app.payments.price_quotes.types import QuoteValue
from app.payments.settlements.enums import OperationKind, OperationStatus
from app.payments.settlements.service import SettlementService
from app.payments.settlements.types import OperationValue
from app.shared.amounts import format_base_units, format_decimal_text
from app.shared.timestamps import format_utc_timestamp

EXPORT_BATCH_SIZE = 200

INVOICE_EXPORT_COLUMNS = [
    "row_kind",
    "occurred_at",
    "invoice_id",
    "invoice_created_at",
    "order_id",
    "invoice_status",
    "partial",
    "late_payment",
    "note",
    "fiat_currency",
    "fiat_amount",
    "chain",
    "asset",
    "amount_due",
    "amount_received",
    "destination_address",
    "deposit_address",
    "paid_at",
    "payment_link_id",
    "row_status",
    "row_asset",
    "row_amount",
    "asset_price_usd",
    "transaction_hash",
    "block_number",
    "from_address",
    "to_address",
]

# A cell starting with one of these is a formula to Excel, LibreOffice and Google Sheets (OWASP CSV Injection),
# including the full-width forms some spreadsheets read as the ASCII ones.
FULL_WIDTH_FORMULA_SIGNS = "\uff1d\uff0b\uff0d\uff20"
FORMULA_LEADING_CHARACTERS = ("=", "+", "-", "@", "\t", "\r", "\n", *FULL_WIDTH_FORMULA_SIGNS)
# Concatenated, not an f-string: semgrep 1.176.1 fails to parse a module holding both an interpolated f-string and a
# ``type`` alias, and --strict fails the house rules on it.
FORMULA_AFTER_SEMICOLON_PATTERN = re.compile(r";(\s*)(?=[=+\-@" + FULL_WIDTH_FORMULA_SIGNS + "])")

type CsvRow = dict[str, str]


def format_csv_time(moment: datetime | None) -> str:
    return format_utc_timestamp(moment) if moment is not None else ""


def format_block_time(block_timestamp: int | None) -> str:
    return format_csv_time(datetime.fromtimestamp(block_timestamp, tz=UTC)) if block_timestamp is not None else ""


def neutralize_formula_text(text: str | None) -> str:
    """Typed text as a spreadsheet shows it, never run: see the module docstring for the rule."""
    if not text:
        return ""
    # A sheet opened with ``;`` as its separator splits here; each piece must not start a formula either.
    text = FORMULA_AFTER_SEMICOLON_PATTERN.sub(r";\1'", text)
    starts_formula = text.startswith(FORMULA_LEADING_CHARACTERS) or text.lstrip().startswith(FORMULA_LEADING_CHARACTERS)
    return f"'{text}" if starts_formula else text


def find_quote_live_at(quotes: list[QuoteValue], *, block_timestamp: int | None) -> QuoteValue | None:
    """The newest quote live at a payment's block time."""
    if block_timestamp is None:
        return None
    moment = datetime.fromtimestamp(block_timestamp, tz=UTC)
    live_quotes = [quote for quote in quotes if quote.is_live_at(moment)]
    return live_quotes[-1] if live_quotes else None


class InvoiceExportBatch:
    """One batch of invoices and everything their rows are built from, read on one session."""

    def __init__(
        self,
        *,
        invoices: list[Invoice],
        payments: list[InvoicePayment],
        stray_payments: list[InvoiceStrayPayment],
        monero_payments: list[MoneroPayment],
        sweep_events: list[DepositSweepEvent],
        operations: list[OperationValue],
        quotes_by_invoice: dict[str, list[QuoteValue]],
    ) -> None:
        self.invoices = invoices
        self.payments_by_invoice = group_by_invoice(payments)
        self.stray_payments_by_invoice = group_by_invoice(stray_payments)
        self.monero_payments_by_invoice = group_by_invoice(monero_payments)
        self.operations_by_invoice: dict[str, list[OperationValue]] = {}
        for operation in operations:
            self.operations_by_invoice.setdefault(operation.invoice_id, []).append(operation)
        self.sweep_events_by_deposit: dict[tuple[str, str], list[DepositSweepEvent]] = {}
        for event in sweep_events:
            self.sweep_events_by_deposit.setdefault((event.chain_key, event.deposit_address), []).append(event)
        self.quotes_by_invoice = quotes_by_invoice


def group_by_invoice[RowWithInvoice: InvoicePayment | InvoiceStrayPayment | MoneroPayment](
    rows: list[RowWithInvoice],
) -> dict[str, list[RowWithInvoice]]:
    grouped: dict[str, list[RowWithInvoice]] = {}
    for row in rows:
        grouped.setdefault(row.invoice_id, []).append(row)
    return grouped


class InvoiceExportService:
    def __init__(
        self, *, session_factory: async_sessionmaker[AsyncSession], redis: Redis, wiring: InvoiceWiring
    ) -> None:
        self.session_factory = session_factory
        self.redis = redis
        self.wiring = wiring
        self.chains_service = wiring.chains_service

    async def stream_account_invoices_csv(
        self,
        account_id: int,
        *,
        status: InvoiceStatus | None,
        created_from: datetime | None,
        created_before: datetime | None,
    ) -> AsyncIterator[str]:
        """The header, then each batch's rows as CSV text (RFC 4180, CRLF line ends), oldest invoice first."""
        yield write_csv_rows([dict(zip(INVOICE_EXPORT_COLUMNS, INVOICE_EXPORT_COLUMNS, strict=True))])
        after: tuple[datetime, str] | None = None
        while True:
            async with self.session_factory() as session:
                # One snapshot per batch: rows of an invoice paid or swept mid-read still agree with each other.
                await session.connection(
                    execution_options={"isolation_level": "REPEATABLE READ", "postgresql_readonly": True}
                )
                batch = await self.read_batch(
                    session,
                    account_id=account_id,
                    status=status,
                    created_from=created_from,
                    created_before=created_before,
                    after=after,
                )
            if not batch.invoices:
                return
            yield write_csv_rows(
                [row for invoice in batch.invoices for row in self.build_invoice_rows(invoice, batch=batch)]
            )
            if len(batch.invoices) < EXPORT_BATCH_SIZE:
                return
            last_invoice = batch.invoices[-1]
            after = (last_invoice.created_at, last_invoice.id)

    async def read_batch(
        self,
        session: AsyncSession,
        *,
        account_id: int,
        status: InvoiceStatus | None,
        created_from: datetime | None,
        created_before: datetime | None,
        after: tuple[datetime, str] | None,
    ) -> InvoiceExportBatch:
        repository = InvoiceRepository(session=session)
        invoices = await repository.list_account_invoices_for_export(
            account_id,
            status=status,
            created_from=created_from,
            created_before=created_before,
            after=after,
            limit=EXPORT_BATCH_SIZE,
        )
        invoice_ids = [invoice.id for invoice in invoices]
        deposit_addresses_by_chain: dict[str, list[str]] = {}
        for invoice in invoices:
            if invoice.deposit_address is not None:
                deposit_addresses_by_chain.setdefault(invoice.chain_key, []).append(invoice.deposit_address)
        sweep_events: list[DepositSweepEvent] = []
        for chain_key, deposit_addresses in deposit_addresses_by_chain.items():
            sweep_events.extend(await repository.list_sweep_events(chain_key, deposit_addresses=deposit_addresses))
        price_quote_service = PriceQuoteService.on_session(
            session,
            redis=self.redis,
            chains_service=self.chains_service,
            chainlink_clients=self.wiring.chainlink_clients,
        )
        settlement_service = SettlementService.on_session(
            session, operator_key_addresses=self.wiring.operator_key_addresses
        )
        return InvoiceExportBatch(
            invoices=invoices,
            payments=await repository.list_payments(invoice_ids),
            stray_payments=await repository.list_stray_payments(invoice_ids),
            monero_payments=await repository.list_monero_payments(invoice_ids),
            sweep_events=sweep_events,
            operations=await settlement_service.list_operations_for_invoices(invoice_ids),
            quotes_by_invoice=await price_quote_service.list_quotes_by_invoice(invoice_ids),
        )

    def build_invoice_rows(self, invoice: Invoice, *, batch: InvoiceExportBatch) -> list[CsvRow]:
        chain = self.chains_service.chains_by_key.get(invoice.chain_key)
        asset = chain.find_asset(invoice.asset_symbol) if chain is not None and invoice.asset_symbol else None
        invoice_columns = build_invoice_columns(invoice, asset=asset)
        rows = [{**invoice_columns, "row_kind": "invoice", "occurred_at": format_csv_time(invoice.created_at)}]
        if chain is None:
            # A chain no longer enabled here: its amounts cannot be read without its decimals.
            return rows
        quotes = batch.quotes_by_invoice.get(invoice.id, [])
        if asset is not None:
            rows.extend(
                build_payment_row(invoice_columns, payment=payment, invoice=invoice, asset=asset, quotes=quotes)
                for payment in batch.payments_by_invoice.get(invoice.id, [])
            )
            rows.extend(
                build_monero_payment_rows(
                    invoice_columns,
                    monero_payments=batch.monero_payments_by_invoice.get(invoice.id, []),
                    invoice=invoice,
                    chain=chain,
                    asset=asset,
                )
            )
        rows.extend(
            build_stray_payment_row(invoice_columns, stray_payment=stray_payment, invoice=invoice, chain=chain)
            for stray_payment in batch.stray_payments_by_invoice.get(invoice.id, [])
        )
        sweep_events = (
            batch.sweep_events_by_deposit.get((invoice.chain_key, invoice.deposit_address), [])
            if invoice.deposit_address is not None
            else []
        )
        operations = batch.operations_by_invoice.get(invoice.id, [])
        rows.extend(build_sweep_rows(invoice_columns, sweep_events=sweep_events, operations=operations, chain=chain))
        rows.extend(build_refund_rows(invoice_columns, operations=operations, chain=chain))
        return rows


def build_invoice_columns(invoice: Invoice, *, asset: AssetConfiguration | None) -> CsvRow:
    return {
        "invoice_id": invoice.id,
        "invoice_created_at": format_csv_time(invoice.created_at),
        "order_id": neutralize_formula_text(invoice.order_id),
        "invoice_status": invoice.status.value,
        "partial": "true" if invoice.partial else "false",
        "late_payment": "true" if invoice.late_payment else "false",
        "note": neutralize_formula_text(invoice.note),
        "fiat_currency": invoice.fiat_currency or "",
        "fiat_amount": format(invoice.fiat_amount, "f") if invoice.fiat_amount is not None else "",
        "chain": invoice.chain_key,
        "asset": asset.symbol if asset is not None else "",
        "amount_due": format_base_units(invoice.amount_due_base_units, decimals=asset.decimals)
        if asset is not None and invoice.amount_due_base_units is not None
        else "",
        "amount_received": format_base_units(invoice.received_base_units, decimals=asset.decimals)
        if asset is not None
        else "",
        "destination_address": invoice.destination_address,
        "deposit_address": invoice.deposit_address or "",
        "paid_at": format_csv_time(invoice.paid_at),
        "payment_link_id": invoice.payment_link_id or "",
    }


def build_payment_row(
    invoice_columns: CsvRow,
    *,
    payment: InvoicePayment,
    invoice: Invoice,
    asset: AssetConfiguration,
    quotes: list[QuoteValue],
) -> CsvRow:
    quote = find_quote_live_at(quotes, block_timestamp=payment.block_timestamp)
    return {
        **invoice_columns,
        "row_kind": "payment",
        "occurred_at": format_block_time(payment.block_timestamp),
        "row_status": "final" if payment.final else "confirming",
        "row_asset": asset.symbol,
        "row_amount": format_base_units(payment.amount_base_units, decimals=asset.decimals),
        "asset_price_usd": format_decimal_text(quote.asset_price_usd) if quote is not None else "",
        "transaction_hash": payment.transaction_hash or "",
        "block_number": str(payment.block_number),
        "from_address": payment.sender or "",
        "to_address": invoice.deposit_address or "",
    }


def build_monero_payment_rows(
    invoice_columns: CsvRow,
    *,
    monero_payments: list[MoneroPayment],
    invoice: Invoice,
    chain: ChainConfiguration,
    asset: AssetConfiguration,
) -> list[CsvRow]:
    """Credited outputs only, as the Invoice resource lists them; a Monero sender is never known (ring signatures)."""
    if not isinstance(chain, MoneroChainConfiguration):
        return []
    confirmations_final = invoice_confirmations_final(invoice, chain=chain)
    return [
        {
            **invoice_columns,
            "row_kind": "payment",
            "occurred_at": format_csv_time(monero_payment.first_seen_at),
            "row_status": "final"
            if is_counted_at_final(
                monero_payment,
                tip_height=invoice.latest_reading_block_number,
                confirmations_final=confirmations_final,
            )
            else "confirming",
            "row_asset": asset.symbol,
            "row_amount": format_base_units(monero_payment.amount_piconero, decimals=asset.decimals),
            "transaction_hash": monero_payment.txid,
            "block_number": str(monero_payment.block_height) if monero_payment.block_height is not None else "",
            "to_address": invoice.deposit_address or "",
        }
        for monero_payment in monero_payments
        if is_credited(monero_payment)
    ]


def build_stray_payment_row(
    invoice_columns: CsvRow, *, stray_payment: InvoiceStrayPayment, invoice: Invoice, chain: ChainConfiguration
) -> CsvRow:
    stray_asset = chain.find_asset(stray_payment.asset_symbol)
    return {
        **invoice_columns,
        "row_kind": "stray_payment",
        "occurred_at": format_block_time(stray_payment.block_timestamp),
        "row_status": "final" if stray_payment.final else "confirming",
        "row_asset": stray_payment.asset_symbol,
        "row_amount": format_base_units(stray_payment.amount_base_units, decimals=stray_asset.decimals)
        if stray_asset is not None
        else "",
        "transaction_hash": stray_payment.transaction_hash or "",
        "block_number": str(stray_payment.block_number),
        "from_address": stray_payment.sender or "",
        "to_address": invoice.deposit_address or "",
    }


def build_sweep_rows(
    invoice_columns: CsvRow,
    *,
    sweep_events: list[DepositSweepEvent],
    operations: list[OperationValue],
    chain: ChainConfiguration,
) -> list[CsvRow]:
    """Every final sweep in every coin (the factory's ``DepositSwept``), then the gateway's sweeps under way or failed,
    which name no coin or amount (they move whatever the deposit holds), as the Invoice resource's ``sweeps``."""
    asset_by_token_address = {asset.token_address: asset for asset in chain.assets}
    rows: list[CsvRow] = []
    for event in sweep_events:
        sweep_asset = asset_by_token_address.get(event.token_address)
        if sweep_asset is None:
            continue
        rows.append(
            {
                **invoice_columns,
                "row_kind": "sweep",
                "occurred_at": format_csv_time(event.created_at),
                "row_status": "final",
                "row_asset": sweep_asset.symbol,
                "row_amount": format_base_units(event.amount_base_units, decimals=sweep_asset.decimals),
                "transaction_hash": event.transaction_hash,
                "block_number": str(event.block_number),
                "from_address": event.deposit_address,
                "to_address": event.destination_address,
            }
        )
    for operation in operations:
        if operation.kind != OperationKind.SWEEP or operation.is_sweep_for_another_key:
            continue
        if not (operation.status.is_active or operation.status == OperationStatus.FAILED):
            continue
        rows.append(
            {
                **invoice_columns,
                "row_kind": "sweep",
                "occurred_at": format_csv_time(operation.created_at),
                "row_status": operation.status.value,
                "transaction_hash": operation.transaction_hash or "",
                "from_address": invoice_columns["deposit_address"],
                "to_address": invoice_columns["destination_address"],
            }
        )
    return rows


def build_refund_rows(
    invoice_columns: CsvRow, *, operations: list[OperationValue], chain: ChainConfiguration
) -> list[CsvRow]:
    """Every refund attempt with its status, as the account view of the Invoice resource lists them."""
    rows: list[CsvRow] = []
    for operation in operations:
        if operation.kind != OperationKind.REFUND:
            continue
        refund_asset = chain.find_asset(operation.asset_symbol or "")
        rows.append(
            {
                **invoice_columns,
                "row_kind": "refund",
                "occurred_at": format_csv_time(operation.created_at),
                "row_status": operation.status.value,
                "row_asset": refund_asset.symbol if refund_asset is not None else (operation.asset_symbol or ""),
                "row_amount": format_base_units(operation.amount_base_units or 0, decimals=refund_asset.decimals)
                if refund_asset is not None
                else "",
                "transaction_hash": operation.transaction_hash or "",
                # Paid from the operator's float, never from the deposit (PLAN.md section 6).
                "to_address": operation.recipient_address or "",
            }
        )
    return rows


def write_csv_rows(rows: list[CsvRow]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=INVOICE_EXPORT_COLUMNS, restval="", lineterminator="\r\n")
    writer.writerows(rows)
    return buffer.getvalue()
