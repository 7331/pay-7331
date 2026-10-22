"""ip-api.com's pro JSON endpoint: the proxy, hosting and country fields only.

Only the pro endpoint: the free one is plain HTTP and 45 requests a minute.

A rejected key answers HTTP 403 with a ``fail`` record whose ``message`` says why
(``{"status":"fail","message":"invalid/expired key, renew at https://members.ip-api.com/order"}``, and ``no API key
supplied, ...`` for none). The body is read before the status, so that reason reaches the caller as
``IpApiFailureKind.KEY_REJECTED``: the pro key is prepaid and "has to be manually renewed"
(https://members.ip-api.com/faq), so it expires unannounced.
"""

import re

import httpx
from pydantic import SecretStr, ValidationError

from app.infrastructure.ip_api.errors import IpApiError, IpApiFailureKind
from app.infrastructure.ip_api.schemas import IpAddressReputation

# ``message`` is "included only when status is fail" (https://ip-api.com/docs/api:json).
REPUTATION_FIELDS = "status,message,countryCode,proxy,hosting"
LOOKUP_TIMEOUT = httpx.Timeout(5.0, connect=3.0)
# The failure messages that are about the key, not the address: "What error message can be returned for a failed
# request?" (https://members.ip-api.com/faq).
KEY_REJECTION_MESSAGE_PREFIXES = (
    "invalid/expired key",
    "no API key supplied",
    "Origin restriction",
    "IP range restriction",
    "Calling IP restriction",
)
KEY_REJECTION_HTTP_STATUSES = frozenset({401, 403})
IP_API_MESSAGE_MAXIMUM_LENGTH = 160
IP_API_MESSAGE_REDACTION = "[redacted]"


def read_redacted_ip_api_message(body: object, *, ip_address: str, api_key: str) -> str | None:
    """The ``message`` of ip-api's ``fail`` record, safe to log: the key and the looked-up address replaced (neither
    is in ip-api's fixed texts, but a body is not ours to trust), control characters dropped, length capped."""
    if not isinstance(body, dict):
        return None
    message = body.get("message")
    if not isinstance(message, str) or not message.strip():
        return None
    for secret_text in (api_key, ip_address):
        if secret_text:
            message = message.replace(secret_text, IP_API_MESSAGE_REDACTION)
    message = re.sub(r"[\x00-\x1f\x7f]", " ", message).strip()
    return message[:IP_API_MESSAGE_MAXIMUM_LENGTH]


class IpApiClient:
    def __init__(self, *, http_client: httpx.AsyncClient, api_key: SecretStr) -> None:
        self.http_client = http_client
        self.api_key = api_key

    async def read_ip_address_reputation(self, ip_address: str) -> IpAddressReputation:
        """``ip_address``'s proxy and hosting flags and country.

        ``ip_address`` is interpolated into the URL, so the caller passes only an address it has parsed. Raises
        ``IpApiError`` on a transport failure, a rejected key, another non-2xx status, a non-``success`` record or an
        unreadable body; its text never holds the key or the address (the request URL, which has both, is not in it)."""
        api_key = self.api_key.get_secret_value()
        try:
            response = await self.http_client.get(
                f"https://pro.ip-api.com/json/{ip_address}",
                params={"key": api_key, "fields": REPUTATION_FIELDS},
                timeout=LOOKUP_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise IpApiError("ip-api is unreachable", failure_kind=IpApiFailureKind.UNREACHABLE) from exc
        try:
            body: object = response.json()
        except ValueError:
            body = None
        ip_api_message = read_redacted_ip_api_message(body, ip_address=ip_address, api_key=api_key)
        if response.status_code in KEY_REJECTION_HTTP_STATUSES or (
            ip_api_message is not None and ip_api_message.startswith(KEY_REJECTION_MESSAGE_PREFIXES)
        ):
            raise IpApiError(
                "ip-api rejected the key", failure_kind=IpApiFailureKind.KEY_REJECTED, ip_api_message=ip_api_message
            )
        if not response.is_success:
            raise IpApiError(
                f"ip-api answered HTTP {response.status_code}",
                failure_kind=IpApiFailureKind.HTTP_STATUS,
                ip_api_message=ip_api_message,
            )
        if not isinstance(body, dict):
            raise IpApiError("ip-api sent an unreadable body", failure_kind=IpApiFailureKind.UNREADABLE)
        if body.get("status") != "success":
            raise IpApiError(
                "ip-api has no record for the address",
                failure_kind=IpApiFailureKind.NO_RECORD,
                ip_api_message=ip_api_message,
            )
        try:
            return IpAddressReputation.model_validate(body)
        except ValidationError as exc:
            raise IpApiError("ip-api record is unreadable", failure_kind=IpApiFailureKind.UNREADABLE) from exc
