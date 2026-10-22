"""The failures the sidecar's client raises. Their text never holds a key or an address."""

from enum import StrEnum


class MoneroSidecarFailureKind(StrEnum):
    # No sidecar on this deployment (``PAY_MONERO_SIDECAR_URL`` empty or no Monero chain enabled).
    NOT_CONFIGURED = "not_configured"
    UNREACHABLE = "unreachable"
    HTTP_STATUS = "http_status"
    UNREADABLE = "unreadable"
    # ``GET /healthz`` lists no tip for the chain: the sidecar does not scan it, or no node answered yet.
    CHAIN_NOT_FOLLOWED = "chain_not_followed"


class MoneroSidecarError(Exception):
    """The sidecar is not set up, was unreachable, answered a status other than 200 or 400, or sent an unreadable
    body."""

    def __init__(self, description: str, *, failure_kind: MoneroSidecarFailureKind) -> None:
        super().__init__(description)
        self.failure_kind = failure_kind


class MoneroSidecarInputRefusedError(Exception):
    """The sidecar answered 400: it could not parse the input at all. Its ``error`` text is not kept, as it may quote
    the input."""
