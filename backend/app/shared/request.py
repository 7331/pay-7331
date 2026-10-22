"""Request helpers: the real client IP behind the host Caddy, and the matched route template.

Caddy sets ``X-Real-IP`` to its resolved client IP. Behind Cloudflare that is
``CF-Connecting-IP``, which the host Caddy reads only from a peer in Cloudflare's ranges (its global
``trusted_proxies`` and ``client_ip_headers``); a
request straight to the origin gets its TCP address. The header is trusted here only from a peer listed in
``PAY_TRUSTED_PROXY_ADDRESSES`` (Caddy's pinned address on the shared proxy network): other containers share that
network, and a private peer is not proof of being Caddy (2026-10-09). Any other caller is identified by its TCP
address. The API never reads ``CF-Connecting-IP`` itself.
"""

import ipaddress

from fastapi import Request
from fastapi.routing import iter_route_contexts
from starlette.requests import HTTPConnection
from starlette.routing import Match

UNKNOWN_CLIENT_IP_ADDRESS = "unknown"


def read_client_ip_address(connection: HTTPConnection, *, trusted_proxy_addresses: frozenset[str]) -> str:
    peer_host = connection.client.host if connection.client is not None else ""
    try:
        peer_address = ipaddress.ip_address(peer_host)
    except ValueError:
        return UNKNOWN_CLIENT_IP_ADDRESS
    if str(peer_address) not in trusted_proxy_addresses:
        return str(peer_address)
    forwarded = connection.headers.get("x-real-ip")
    if forwarded is None:
        return str(peer_address)
    try:
        return str(ipaddress.ip_address(forwarded.strip()))
    except ValueError:
        return str(peer_address)


def route_template(request: Request) -> str:
    """The matched route's full path template (``/api/v1/invoices/{invoice_id}``), never the path the caller sent.

    A request refused before routing (the rate limiter's 429) has no route in its scope; it is matched here the way
    the router would have, for the log line only.
    """
    route = request.scope.get("route")
    if route is not None:
        return route.path
    for context in iter_route_contexts(request.app.router.routes) if "app" in request.scope else ():
        if context.path is not None and context.matches(request.scope)[0] == Match.FULL:
            return context.path
    return f"{request.method} (unrouted)"
