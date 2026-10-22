"""Typed log records of the account event stream. Ids only: never an event's data."""

import logging

from app.shared.logging import LogEvent


class AccountEventStreamSubscriberDropped(LogEvent):
    level = logging.WARNING
    message = "An event stream fell too far behind and was closed; its page reconnects with Last-Event-ID"
    operation = "account_event_stream"
    account_id: int
    queued_event_count: int


class AccountEventStreamSlotsUnavailable(LogEvent):
    level = logging.WARNING
    message = "Could not reach Redis for the event stream slots; the stream opened uncounted"
    operation = "account_event_stream_slot"
    step: str
