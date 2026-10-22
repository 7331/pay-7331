"""Wire body of an account event."""

from datetime import datetime

from pydantic import JsonValue

from app.payments.account_events.enums import AccountEventType
from app.shared.schemas import ApiResponse


class AccountEventResponse(ApiResponse):
    # `evt_...`: random, so ids say nothing about volume; order events by `created_at`.
    id: str
    type: AccountEventType
    # None for an event about the account itself (`account.payout_address_changed`).
    invoice_id: str | None
    created_at: datetime
    # The account's Invoice resource at the change; for `account.payout_address_changed`
    # `{"previous_payout_address", "payout_address"}`.
    data: JsonValue


class AccountEventsCaughtUpResponse(ApiResponse):
    """Sent once the backlog is out: what follows is new."""

    # The newest event the stream sent or resumed after; null when there is none yet.
    latest_event_id: str | None


class AccountEventPageResponse(ApiResponse):
    """``GET /events``: newest first; ``next_cursor`` (the page's last event id) fetches the older page, null once
    there is none."""

    events: list[AccountEventResponse]
    next_cursor: str | None
