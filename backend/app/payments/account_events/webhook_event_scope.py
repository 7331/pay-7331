"""Which account events the account webhook sends, and so which an API token may read on ``GET /events`` (2026-10-21):
one rule, written once in its SQL half (the token's event list) and once in its Python half (the webhook pass), side by
side so they cannot drift. An event is in scope when it names an invoice, or its type starts with one of
``WEBHOOK_EVENT_TYPE_PREFIXES_WITHOUT_INVOICE`` (a static deposit address's ``static_address.*``). Out of scope stay ``operator.alert`` and the ``account.*`` events,
which name the operator's problems or the account's addresses. Discord keeps its own filter
(payments/account_webhooks/discord_service.py). PLAN.md section 8.3."""

from sqlalchemy import ColumnElement, or_

from app.payments.account_events.models import AccountEvent

# Every static deposit address event's type starts with it (PLAN.md section 17); the domain's enums reuse it.
STATIC_DEPOSIT_ADDRESS_EVENT_PREFIX = "static_address."
# Event types the webhook sends though they name no invoice; a new kind of event joins by its type prefix here.
WEBHOOK_EVENT_TYPE_PREFIXES_WITHOUT_INVOICE: tuple[str, ...] = (STATIC_DEPOSIT_ADDRESS_EVENT_PREFIX,)


def is_webhook_event(*, invoice_id: str | None, event_type: str) -> bool:
    """The Python half: whether the webhook sends this event (``str.startswith`` of an empty tuple is False)."""
    return invoice_id is not None or event_type.startswith(WEBHOOK_EVENT_TYPE_PREFIXES_WITHOUT_INVOICE)


def webhook_event_condition() -> ColumnElement[bool]:
    """The SQL half of ``is_webhook_event``, for a query on ``account_events`` (``autoescape``: ``_`` and ``%`` in a
    prefix match themselves, not any character)."""
    return or_(
        AccountEvent.invoice_id.is_not(None),
        *(
            AccountEvent.event_type.startswith(prefix, autoescape=True)
            for prefix in WEBHOOK_EVENT_TYPE_PREFIXES_WITHOUT_INVOICE
        ),
    )
