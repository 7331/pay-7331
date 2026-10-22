"""The SDK's exceptions. Every API refusal is HTTP status plus ``{"code", "message"}`` (PLAN.md section 7.1), raised
as the ``PayApiError`` subclass of its status, so a caller can catch one class or branch on ``.code``."""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import re

# Delay-seconds is 1*DIGIT (RFC 9110 section 10.2.3); ten digits is over 300 years, far past any sane wait.
RETRY_AFTER_SECONDS_PATTERN = re.compile(r"[0-9]{1,10}")


class PayError(Exception):
    """Base of everything this SDK raises."""


class PayTransportError(PayError):
    """No HTTP answer: a timeout, a refused connection, a TLS failure. The message names the httpx error with the API
    token redacted; the httpx error itself is not chained (it may quote the request's headers). A request that timed
    out may still have reached the API: retry a create only with an ``order_id`` or ``customer_reference``, which make
    it idempotent."""


class PayResponseError(PayError):
    """A 2xx answer that is not the shape this SDK reads: not JSON, or a field missing or of another type. The
    pydantic ``ValidationError`` is the ``__cause__``. A proxy in the way or an SDK far older than the API."""


class PayEventDataError(PayError):
    """An event's ``data`` that does not parse as this SDK's model. ``data`` is a snapshot frozen when the event was
    recorded, so an event older than a field the model needs lacks it: read the resource now instead
    (``read_invoice(event.invoice_id)``, or ``read_static_deposit_address`` with the address id). The pydantic
    ``ValidationError`` is the ``__cause__``."""


class PayApiError(PayError):
    """The API answered with a non-2xx status.

    ``code`` is the API's stable machine code (``invoice_not_found``, ``api_token_invalid``, ``rate_limited``, ...),
    ``message`` its human sentence, ``status_code`` the HTTP status. ``retry_after`` is the ``Retry-After`` header in
    seconds when the API sent one (429 and some 503s), else None. An answer that is not the API's JSON shape (a proxy's
    HTML 502) gets ``code`` ``"http_error"`` and the body's start as ``message``."""

    def __init__(self, *, status_code: int, code: str, message: str, retry_after: float | None = None) -> None:
        super().__init__(f"{status_code} {code}: {message}")
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retry_after = retry_after


class PayValidationError(PayApiError):
    """400: the request was refused as invalid (``validation_error``, ``asset_not_supported``, ...)."""


class PayAuthenticationError(PayApiError):
    """401: the API token is malformed, unknown or revoked (``api_token_invalid``), or none was sent."""


class PayPermissionError(PayApiError):
    """403: not allowed (``forbidden``)."""


class PayNotFoundError(PayApiError):
    """404: no such resource on this account; another account's id is 404 too."""


class PayConflictError(PayApiError):
    """409: the resource's state refuses the change (``invoice_state_conflict``, ``webhook_not_set``, ``sold_out``,
    ``invoice_not_refreshable``, ...)."""


class PayRateLimitedError(PayApiError):
    """429: ``rate_limited``, ``too_many_open_invoices`` or ``public_invoice_capacity_reached``; wait ``retry_after``
    seconds when it is set."""


class PayServerError(PayApiError):
    """5xx: the API or something behind it failed (``service_unavailable``, ``internal_error``)."""


class WebhookVerificationError(PayError):
    """A webhook delivery whose headers are missing or malformed, whose signature matches no ``v1`` signature under
    the secret, whose timestamp is outside the tolerance, or whose signed body is not a pay-7331 event. Answer it
    with a 4xx and never act on it."""


API_ERROR_CLASS_BY_STATUS: dict[int, type[PayApiError]] = {
    400: PayValidationError,
    401: PayAuthenticationError,
    403: PayPermissionError,
    404: PayNotFoundError,
    409: PayConflictError,
    429: PayRateLimitedError,
}


def api_error_class_for_status(status_code: int) -> type[PayApiError]:
    if status_code >= 500:
        return PayServerError
    return API_ERROR_CLASS_BY_STATUS.get(status_code, PayApiError)


def parse_retry_after_seconds(header_value: str | None, *, now: datetime | None = None) -> float | None:
    """``Retry-After`` is either delay-seconds or an HTTP-date (RFC 9110 section 10.2.3,
    https://www.rfc-editor.org/rfc/rfc9110#section-10.2.3); pay-7331 sends seconds. None when absent or unreadable."""
    if header_value is None:
        return None
    text = header_value.strip()
    if RETRY_AFTER_SECONDS_PATTERN.fullmatch(text):
        return float(text)
    try:
        retry_at = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    current_time = now if now is not None else datetime.now(tz=timezone.utc)
    return max(0.0, (retry_at - current_time).total_seconds())
