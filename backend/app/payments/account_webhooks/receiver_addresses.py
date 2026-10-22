"""Where a webhook may be sent (SSRF guard, 2026-10-12).

The URL is the account's to choose, so the API must never be made to POST into its own networks. When saved, a URL
must be https with a host and no user info, and an IP literal must be global. At send time the host is resolved, every
address it resolves to must be global (``ipaddress``'s ``is_global``: allocated for public networks in IANA's special-
purpose registries, which rules out loopback, private, link-local, shared, documentation and reserved ranges;
https://docs.python.org/3/library/ipaddress.html#ipaddress.IPv4Address.is_global) and not multicast, and the request
then connects to the checked address itself, with the original ``Host`` and, for https, the hostname for SNI and
certificate verification (httpx's ``sni_hostname`` extension,
https://www.python-httpx.org/advanced/extensions/#sni_hostname). So a DNS answer that changes between check and
connect (rebinding) cannot reach an unchecked address. Standard Webhooks asks for exactly this filtering
(https://github.com/standard-webhooks/standard-webhooks/blob/main/spec/standard-webhooks.md, "SSRF").
``PAY_WEBHOOK_ALLOW_PRIVATE_HOSTS`` (the e2e stack only) lifts the address check and allows http.
"""

import asyncio
import ipaddress
import socket
from urllib.parse import SplitResult, urlsplit

from pydantic import BaseModel, ConfigDict

from app.payments.account_webhooks.enums import AccountWebhookFailureReason
from app.payments.account_webhooks.models import WEBHOOK_URL_MAXIMUM_LENGTH
from app.shared.errors import InvalidInputError

RESOLVE_TIMEOUT_SECONDS = 5.0
DEFAULT_PORTS = {"https": 443, "http": 80}


class ReceiverAddressRefusedError(Exception):
    def __init__(self, reason: AccountWebhookFailureReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


class CheckedReceiverAddress(BaseModel):
    """A webhook URL resolved and checked: connect to ``request_url`` (the checked IP), send ``host_header``, verify
    TLS against ``hostname``."""

    model_config = ConfigDict(frozen=True)

    request_url: str
    host_header: str
    hostname: str
    scheme: str


def is_global_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_global and not address.is_multicast


def split_webhook_url(webhook_url: str, *, allow_private_hosts: bool) -> SplitResult:
    """The URL's parts, or 400 naming what is wrong with it."""
    allowed_schemes = {"https", "http"} if allow_private_hosts else {"https"}
    if len(webhook_url) > WEBHOOK_URL_MAXIMUM_LENGTH or any(
        character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F for character in webhook_url
    ):
        raise InvalidInputError("url: an https:// URL of at most 2048 characters, no spaces")
    try:
        parts = urlsplit(webhook_url)
        hostname = parts.hostname
        _port = parts.port
    except ValueError as parse_error:
        raise InvalidInputError("url: not a URL") from parse_error
    if parts.scheme not in allowed_schemes or not hostname:
        raise InvalidInputError("url: an https:// URL with a host is required")
    if parts.username is not None or parts.password is not None or parts.fragment:
        raise InvalidInputError("url: no user name, password or #fragment")
    try:
        literal_address = ipaddress.ip_address(hostname)
    except ValueError:
        literal_address = None
    if literal_address is not None and not allow_private_hosts and not is_global_address(literal_address):
        raise InvalidInputError("url: that address is not on the public internet")
    if not allow_private_hosts and (hostname == "localhost" or hostname.endswith(".localhost")):
        raise InvalidInputError("url: that address is not on the public internet")
    return parts


async def resolve_receiver_address(webhook_url: str, *, allow_private_hosts: bool) -> CheckedReceiverAddress:
    """Resolves the URL's host now and checks every address; refuses with the reason a delivery records."""
    parts = split_webhook_url(webhook_url, allow_private_hosts=allow_private_hosts)
    hostname = parts.hostname or ""
    port = parts.port or DEFAULT_PORTS[parts.scheme]
    try:
        async with asyncio.timeout(RESOLVE_TIMEOUT_SECONDS):
            address_infos = await asyncio.get_running_loop().getaddrinfo(
                hostname, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
            )
    except (OSError, TimeoutError) as resolve_error:
        raise ReceiverAddressRefusedError(AccountWebhookFailureReason.UNRESOLVABLE_HOST) from resolve_error
    addresses = list(
        dict.fromkeys(ipaddress.ip_address(str(address_info[4][0]).split("%", 1)[0]) for address_info in address_infos)
    )
    if not addresses:
        raise ReceiverAddressRefusedError(AccountWebhookFailureReason.UNRESOLVABLE_HOST)
    if not allow_private_hosts and not all(is_global_address(address) for address in addresses):
        raise ReceiverAddressRefusedError(AccountWebhookFailureReason.REFUSED_ADDRESS)
    checked_address = addresses[0]
    address_text = f"[{checked_address}]" if checked_address.version == 6 else str(checked_address)
    path_and_query = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    host_text = f"[{hostname}]" if ":" in hostname else hostname
    return CheckedReceiverAddress(
        request_url=f"{parts.scheme}://{address_text}:{port}{path_and_query}",
        host_header=host_text if parts.port is None else f"{host_text}:{port}",
        hostname=hostname,
        scheme=parts.scheme,
    )
