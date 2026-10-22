"""A route's ``responses=``: each error status it can answer with and the codes behind it, read from the error classes
it raises, so the OpenAPI document names the real refusals (404, 409, 503 ...) and never a hand-copied code. The
components referenced here are installed by ``app.openapi_contract``."""

from typing import Any

from app.shared.errors import AppError

ERROR_RESPONSE_SCHEMA_NAME = "ErrorResponse"
RETRY_AFTER_HEADER_NAME = "RetryAfter"
# JSON whatever the route's own media type: an SVG or CSV route refuses with the same error body as every other.
ERROR_RESPONSE_CONTENT: dict[str, Any] = {
    "application/json": {"schema": {"$ref": f"#/components/schemas/{ERROR_RESPONSE_SCHEMA_NAME}"}}
}
RETRY_AFTER_HEADER: dict[str, Any] = {
    "description": "Seconds to wait before trying again.",
    "schema": {"type": "integer", "minimum": 1},
}
RATE_LIMITED_HEADERS: dict[str, Any] = {"Retry-After": {"$ref": f"#/components/headers/{RETRY_AFTER_HEADER_NAME}"}}
RATE_LIMITED_STATUS = 429


def describe_error_responses(*error_classes: type[AppError]) -> dict[int | str, dict[str, Any]]:
    """One response per status, in status order, listing every code of that status in the order given."""
    codes_by_status: dict[int, list[str]] = {}
    for error_class in error_classes:
        codes_of_status = codes_by_status.setdefault(error_class.status, [])
        if error_class.code not in codes_of_status:
            codes_of_status.append(error_class.code)
    error_responses: dict[int | str, dict[str, Any]] = {}
    for status, codes in sorted(codes_by_status.items()):
        error_response: dict[str, Any] = {
            "description": "Codes: " + ", ".join(f"`{code}`" for code in codes) + ".",
            "content": ERROR_RESPONSE_CONTENT,
        }
        if status == RATE_LIMITED_STATUS:
            error_response["headers"] = RATE_LIMITED_HEADERS
        error_responses[status] = error_response
    return error_responses
