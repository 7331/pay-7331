"""The error base and the shared errors.

Every application error carries a stable client-facing ``code`` and an HTTP ``status``; the message is safe
to show. ``app.shared.exception_handlers`` maps them to one JSON shape.
"""

from typing import ClassVar


class AppError(Exception):
    """Base application error with a client-safe code and HTTP status."""

    code: str = "internal_error"
    status: int = 500
    retry_after_seconds: int | None = None
    # Extra response headers a refusal must carry (a 401 to a bearer token: ``WWW-Authenticate``).
    response_headers: ClassVar[tuple[tuple[str, str], ...]] = ()

    def __init__(self, message: str | None = None, *, retry_after_seconds: int | None = None) -> None:
        self.message = message if message is not None else self.code
        if retry_after_seconds is not None:
            self.retry_after_seconds = retry_after_seconds
        super().__init__(self.message)


class NotFoundError(AppError):
    code = "not_found"
    status = 404


class InvalidInputError(AppError):
    code = "validation_error"
    status = 400


class RateLimitedError(AppError):
    code = "rate_limited"
    status = 429


class ServiceUnavailableError(AppError):
    code = "service_unavailable"
    status = 503


class UnauthorizedError(AppError):
    code = "unauthorized"
    status = 401


class ConflictError(AppError):
    code = "conflict"
    status = 409


class ForbiddenError(AppError):
    code = "forbidden"
    status = 403
