"""Map application and Redis errors to one JSON shape; an unhandled
error gets the same shape from ``app.unhandled_error_middleware``."""

from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import iter_route_contexts
from fastapi.utils import is_body_allowed_for_status_code
from redis.exceptions import RedisError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.shared import logging as log
from app.shared.errors import AppError, InvalidInputError, ServiceUnavailableError
from app.shared.log_events import RedisUnavailable
from app.shared.request import route_template

logger = log.get_logger(__name__)

NO_STORE: dict[str, str] = {"Cache-Control": "no-store"}
# The code of a router error whose status the standard library does not name.
UNNAMED_HTTP_ERROR_CODE = "http_error"


def app_error_response(exc: AppError) -> JSONResponse:
    """The one error wire shape, shared by the handlers and the rate limit middleware."""
    headers = dict(NO_STORE) | dict(exc.response_headers)
    if exc.retry_after_seconds is not None:
        headers["Retry-After"] = str(exc.retry_after_seconds)
    return JSONResponse(status_code=exc.status, content={"code": exc.code, "message": exc.message}, headers=headers)


def name_http_error_code(status_code: int) -> str:
    """``not_found`` for 404, ``method_not_allowed`` for 405, ``bad_request`` for 400: the status phrase in snake
    case."""
    try:
        return HTTPStatus(status_code).phrase.lower().replace(" ", "_").replace("-", "_")
    except ValueError:
        return UNNAMED_HTTP_ERROR_CODE


def list_allowed_methods(request: Request) -> str:
    """Every method some route serves at the request's path. Starlette's 405 names only the first route that matched
    the path, and here one path is often several routes, one per method."""
    allowed_methods: set[str] = set()
    for route_context in iter_route_contexts(request.app.routes):
        path_regex = getattr(route_context, "path_regex", None)
        if path_regex is not None and route_context.methods and path_regex.match(request.url.path):
            allowed_methods.update(route_context.methods)
    return ", ".join(sorted(allowed_methods))


def http_exception_response(exc: StarletteHTTPException, *, request: Request) -> Response:
    headers = dict(NO_STORE) | dict(exc.headers or {})
    if exc.status_code == 405:
        headers["Allow"] = list_allowed_methods(request)
    if not is_body_allowed_for_status_code(exc.status_code):
        return Response(status_code=exc.status_code, headers=headers)
    content = {"code": name_http_error_code(exc.status_code), "message": str(exc.detail).lower()}
    return JSONResponse(status_code=exc.status_code, content=content, headers=headers)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(_request: Request, exc: AppError) -> JSONResponse:
        return app_error_response(exc)

    # Starlette's, so the router's own errors are caught too, not only FastAPI's subclass
    # (https://fastapi.tiangolo.com/tutorial/handling-errors/#override-the-httpexception-error-handler): an unknown API
    # path is 404 ``not_found``, a known one with another method 405 ``method_not_allowed`` with every allowed method in
    # ``Allow``, a body that cannot be parsed 400 ``bad_request``.
    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> Response:  # noqa: PLR0917
        return http_exception_response(exc, request=request)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        first_error = exc.errors()[0] if exc.errors() else None
        field = ""
        if first_error:
            # The request's own field, not the deepest part: ``loc`` is the source (body, query, header, path), then the
            # field, then whatever lies inside it (a metadata key, a list index).
            location = first_error["loc"]
            field = str(location[1] if len(location) > 1 else location[0]) if location else ""
        return app_error_response(InvalidInputError(f"{field}: invalid value" if field else "invalid request"))

    @app.exception_handler(RedisError)
    async def handle_redis_error(request: Request, exc: RedisError) -> JSONResponse:  # noqa: PLR0917
        log.emit(logger, event=RedisUnavailable(route=route_template(request)), exception=exc)
        return app_error_response(ServiceUnavailableError("a backing service is unavailable right now"))
