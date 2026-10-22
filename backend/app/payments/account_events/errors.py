"""Account event stream refusals, each a stable code the pages map to their own copy."""

from app.shared.errors import RateLimitedError


class TooManyAccountEventStreamsError(RateLimitedError):
    """The session already has ``MAXIMUM_OPEN_STREAMS_PER_ACCOUNT_SESSION`` event streams open
    (``stream_slot_store.py``)."""

    code = "too_many_event_streams"
