"""Typed log records of the invoices domain. Ids and public amounts only; never metadata or a client IP."""

import logging

from app.shared.logging import LogEvent


class InvoiceCreated(LogEvent):
    """On EVM with the deposit and destination (both public on chain, never the salt or seed): ``app.cli
    recover-deposit --from-log`` re-derives each salt from these lines if Postgres is lost (PLAN.md section 3,
    2026-10-12). On Monero with the wallet id and subaddress minor instead, never an address: a subaddress links a
    payer to a receiver's wallet, and there is no deposit to recover (recover-deposit skips lines with no deposit)."""

    message = "Invoice created"
    operation = "invoice_create"
    invoice_id: str
    chain_key: str
    deposit_address: str | None = None
    destination_address: str | None = None
    monero_wallet_id: int | None = None
    subaddress_minor: int | None = None


class InvoiceDepositDerived(LogEvent):
    """The deposit of an invoice that offered several chains, derived at the payer's pick (PLAN.md section 15): the
    same facts ``InvoiceCreated`` names for one chain, so ``app.cli recover-deposit --from-log`` finds the deposit
    again (deposit_recovery_service.py reads both operations). Never the salt or seed."""

    message = "Invoice deposit derived at the payer's pick"
    operation = "invoice_deposit_derive"
    invoice_id: str
    chain_key: str
    deposit_address: str
    destination_address: str


class InvoiceRefreshed(LogEvent):
    """An expired, unpaid invoice of an order replaced by a new one (``refresh_expired``, 2026-10-21); the new one's
    own ``InvoiceCreated`` line names its deposit. Ids only, never the order id."""

    message = "Expired invoice refreshed into a new one"
    operation = "invoice_refresh"
    invoice_id: str
    refreshed_from_invoice_id: str


class InvoiceStatusChanged(LogEvent):
    message = "Invoice status changed"
    operation = "invoice_status"
    invoice_id: str
    previous_status: str
    status: str


class LatePaymentFound(LogEvent):
    message = "Money found on a closed invoice or in an asset it did not ask for"
    operation = "invoice_late_payment"
    invoice_id: str
    asset_symbol: str
    # The part of the held balance the invoice had not counted (credited or recorded stray money).
    late_base_units: str


class OverpayRefundRecorded(LogEvent):
    message = "Overpay excess sent back to its sender automatically once final"
    operation = "invoice_auto_refund"
    invoice_id: str
    operation_id: int


class StrayRefundRecorded(LogEvent):
    message = "Money in an asset the invoice did not ask for sent back to its sender automatically once final"
    operation = "invoice_stray_auto_refund"
    invoice_id: str
    operation_id: int
    asset_symbol: str


class AutomaticRefundDeferred(LogEvent):
    level = logging.WARNING
    message = "Automatic refund not decided for want of a price; the watcher decides it again every tick"
    operation = "invoice_auto_refund_deferred"
    invoice_id: str
    asset_symbols: str


class AutomaticRefundDecidedAfterDeferral(LogEvent):
    message = "Deferred automatic refund decided once a price was readable"
    operation = "invoice_auto_refund_deferred"
    invoice_id: str
    deferred_seconds: int


class StrayPaymentRecordedFromTransaction(LogEvent):
    message = "Stray payment recorded by the operator from a transaction hash"
    operation = "invoice_stray_payment_from_transaction"
    invoice_id: str
    transaction_hash: str
    asset_symbol: str


class SenderLessStrayRowAttributed(LogEvent):
    message = "Stray money recorded with no sender attributed to a proven transaction"
    operation = "invoice_stray_payment_from_transaction"
    invoice_id: str
    asset_symbol: str
    absorbed_base_units: str


class StrayPaymentFinal(LogEvent):
    message = "Final money in an asset the invoice did not ask for; shown, never counted"
    operation = "invoice_stray_payment"
    invoice_id: str


class InvoicePaymentChecked(LogEvent):
    message = "The payer's I-paid check answered"
    operation = "invoice_payment_check"
    invoice_id: str
    outcome: str
    # Whether the payer pasted a transaction hash; never the hash or anything else from the request body.
    with_transaction_hash: bool


class MoneroPaymentCredited(LogEvent):
    message = "A Monero output in a block, agreed by a second node operator, was credited to its invoice"
    operation = "monero_credit"
    invoice_id: str
    monero_payment_id: int


class MoneroPaymentUncredited(LogEvent):
    level = logging.WARNING
    message = "A credited Monero output's block left the chain (a reorg); its credit was taken back"
    operation = "monero_credit"
    invoice_id: str
    monero_payment_id: int


class MoneroLatePaymentCredited(LogEvent):
    level = logging.WARNING
    message = "A Monero output reached a closed invoice; it is marked late and the account told, nothing else changes"
    operation = "monero_credit"
    invoice_id: str
    monero_payment_id: int


class MoneroPaymentHeld(LogEvent):
    level = logging.WARNING
    message = "A Monero output's check on a second node operator disagreed; the invoice is held at confirming"
    operation = "monero_credit"
    invoice_id: str


class PublicInvoiceCapacityRefused(LogEvent):
    """A public invoice refused because a chain it offers already has ``PAY_MAXIMUM_OPEN_PUBLIC_INVOICES_PER_CHAIN``
    public invoices waiting for a coin or a payment: a flood, or a cap set too low. Never the client network."""

    level = logging.WARNING
    message = "Public invoice refused: the chain's public invoice capacity is reached"
    operation = "invoice_create"
    chain_key: str
    waiting_public_invoice_count: int
