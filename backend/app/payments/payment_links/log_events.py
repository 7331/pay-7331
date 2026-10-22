"""Typed log records of the payment_links domain. Ids and counts only; never a note or a client IP."""

from app.shared.logging import LogEvent


class PaymentLinkCreated(LogEvent):
    message = "Quantity link created"
    operation = "payment_link_create"
    payment_link_id: str
    chain_key: str
    quantity: int


class PaymentLinkInvoiceCreated(LogEvent):
    message = "A payer took a slot of a quantity link"
    operation = "payment_link_invoice"
    payment_link_id: str
    invoice_id: str
    slots_held: int
    quantity: int


class PaymentLinkSoldOutRefused(LogEvent):
    message = "A payer was refused: every slot of the quantity link is paid or held"
    operation = "payment_link_invoice"
    payment_link_id: str
    quantity: int


class PaymentLinkClosed(LogEvent):
    message = "Quantity link closed by its account"
    operation = "payment_link_close"
    payment_link_id: str
