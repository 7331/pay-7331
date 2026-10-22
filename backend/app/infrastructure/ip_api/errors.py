"""The one failure ip-api's client raises, with what kind it is and ip-api's own words."""

from enum import StrEnum


class IpApiFailureKind(StrEnum):
    UNREACHABLE = "unreachable"
    # The key is invalid, expired, missing or restricted against this caller: every lookup fails until it is fixed.
    KEY_REJECTED = "key_rejected"
    HTTP_STATUS = "http_status"
    NO_RECORD = "no_record"
    UNREADABLE = "unreadable"


class IpApiError(Exception):
    """ip-api.com was unreachable, rejected the key, answered another non-2xx status or a non-success record, or sent
    an unreadable body.

    ``ip_api_message`` is the ``message`` of ip-api's ``fail`` record (fixed vendor text such as ``invalid/expired
    key, renew at https://members.ip-api.com/order``), with the key and the looked-up address redacted, so it may be
    logged and shown to the operator; None when the body had none."""

    def __init__(self, description: str, *, failure_kind: IpApiFailureKind, ip_api_message: str | None = None) -> None:
        super().__init__(description)
        self.failure_kind = failure_kind
        self.ip_api_message = ip_api_message
