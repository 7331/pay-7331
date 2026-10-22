"""Server-minted request correlation, the id every log line of one request carries.

The id is minted per request; an inbound ``X-Request-ID`` is never read, so a client cannot fabricate a grouping.
The minted id is returned in ``X-Request-ID`` for support to grep; asgi-correlation-id's contextvar carries it to
``app.shared.logging``.
"""

from uuid import uuid4

from asgi_correlation_id.context import correlation_id
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class ServerMintedCorrelationIdMiddleware:
    """Set a fresh 32-hex uuid4 as the request's correlation id and stamp it on the response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:  # noqa: PLR0917  # ASGI interface
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        minted = uuid4().hex
        correlation_id.set(minted)

        async def send_with_correlation_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                MutableHeaders(scope=message).append("X-Request-ID", minted)
            await send(message)

        await self.app(scope, receive, send_with_correlation_header)
