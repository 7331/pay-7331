"""Answer an unhandled error with the one 500 JSON shape, and end it there.

Starlette's handler for ``Exception`` answers and then re-raises, and uvicorn closes a connection whose app raised
after its response started: the client's next keep-alive request on it (Caddy's, for a later visitor) was reset. Seen
in the live e2e (2026-10-09). Pure ASGI, registered inside the request log (``app.main``) so the 500 is logged as a
request with its correlation id. An error after the response started still goes up, since nothing can be sent then.
"""

from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.shared import logging as log
from app.shared.errors import AppError
from app.shared.exception_handlers import app_error_response
from app.shared.log_events import UnhandledError
from app.shared.request import route_template

logger = log.get_logger(__name__)


class UnhandledErrorMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:  # noqa: PLR0917  # ASGI interface
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        response_started = False

        async def send_tracking_start(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, send_tracking_start)
        except Exception:
            if response_started:
                raise
            log.exception(logger, event=UnhandledError(route=route_template(Request(scope))))
            await app_error_response(AppError("internal server error"))(scope, receive, send)
