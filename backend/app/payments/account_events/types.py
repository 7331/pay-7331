"""Read types of account events."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.payments.account_events.schemas import AccountEventResponse


class ClaimedAccountEvent(BaseModel):
    """An event webhook delivery took (``AccountEventService.claim_account_events_for_webhooks``) and its account."""

    model_config = ConfigDict(frozen=True)

    account_id: int
    # The internal key, which deliveries reference; the response carries the public id.
    account_event_id: int
    account_event_response: AccountEventResponse


class StreamedAccountEvent(BaseModel):
    """An event on its way to open streams: the internal id orders and dedupes, the response is what is sent."""

    model_config = ConfigDict(frozen=True)

    account_event_id: int
    created_at: datetime
    account_event_response: AccountEventResponse
