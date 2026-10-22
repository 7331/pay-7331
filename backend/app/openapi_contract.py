"""The OpenAPI document made to match what the server really sends.

FastAPI writes a 422 with its own error list for every validated request, while the validation handler in
``app.shared.exception_handlers`` answers 400 ``validation_error`` with the ``{code, message}`` body every error has.
It also knows nothing of the refusals the auth dependencies and the rate limiter send. ``install_openapi_contract``
fixes the generated document once, at the composition root: the shared error body and the shared refusals are
declared once under ``components`` and referenced from each operation, and the ``title`` keywords pydantic writes
everywhere are dropped.

The document is not served (``openapi_url=None`` in ``app.main``); ``tools/dump_openapi.py`` writes it to a file.
"""

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, iter_route_contexts

from app.payments.accounts.dependencies import find_optional_account_access
from app.rate_limit_middleware import UNLIMITED_PATHS, UNLIMITED_PREFIX
from app.shared.openapi_error_responses import (
    ERROR_RESPONSE_CONTENT,
    ERROR_RESPONSE_SCHEMA_NAME,
    RATE_LIMITED_HEADERS,
    RETRY_AFTER_HEADER,
    RETRY_AFTER_HEADER_NAME,
)
from app.shared.schemas import ErrorResponse

VALIDATION_FAILED_RESPONSE_NAME = "ValidationFailed"
UNAUTHORIZED_RESPONSE_NAME = "Unauthorized"
FORBIDDEN_RESPONSE_NAME = "Forbidden"
RATE_LIMITED_RESPONSE_NAME = "RateLimited"

# The session's Origin check runs only on methods that change something, so only those can be refused with 403.
SAFE_METHODS = frozenset({"get", "head"})

SHARED_ERROR_RESPONSES: dict[str, dict[str, Any]] = {
    VALIDATION_FAILED_RESPONSE_NAME: {
        "description": "The request's path, query or body is not valid; code `validation_error`.",
        "content": ERROR_RESPONSE_CONTENT,
    },
    UNAUTHORIZED_RESPONSE_NAME: {
        "description": "No credentials, or a session or API token that is unknown, expired or revoked.",
        "headers": {
            "WWW-Authenticate": {
                "description": (
                    "The challenge: `Bearer` on the routes that take an API token, else `Cookie` naming the session"
                    " cookie."
                ),
                "schema": {"type": "string"},
            }
        },
        "content": ERROR_RESPONSE_CONTENT,
    },
    FORBIDDEN_RESPONSE_NAME: {
        "description": "Signed in, but not allowed: a session's change sent from another origin, for one.",
        "content": ERROR_RESPONSE_CONTENT,
    },
    RATE_LIMITED_RESPONSE_NAME: {
        "description": "Over a request budget; code `rate_limited`.",
        "headers": RATE_LIMITED_HEADERS,
        "content": ERROR_RESPONSE_CONTENT,
    },
}


def name_operation_after_handler(route: APIRoute) -> str:
    """The handler's own name as the operation id, so a generated client's functions read like the server's."""
    return route.name


def strip_schema_titles(openapi_schema: dict[str, Any]) -> None:
    """Drops the ``title`` keyword pydantic writes on every model and field: it repeats the name it sits under and
    no generated client reads it. Only ``paths`` and ``components`` are walked, so the document's own ``info.title``
    stays."""
    for section_name in ("paths", "components"):
        strip_titles_below(openapi_schema.get(section_name))


def strip_titles_below(node: object) -> None:
    """The walk of ``strip_schema_titles``. A ``properties`` map's keys are property names, not keywords, so a
    property called ``title`` is stepped over and kept."""
    match node:
        case dict():
            strip_title_keyword(node)
            for key, value in node.items():
                match key, value:
                    case "properties", dict():
                        for property_schema in value.values():
                            strip_titles_below(property_schema)
                    case _:
                        strip_titles_below(value)
        case list():
            for item in node:
                strip_titles_below(item)


def strip_title_keyword(schema_object: dict[str, object]) -> None:
    match schema_object.get("title"):
        case str():
            del schema_object["title"]


def is_rate_limited_path(path: str) -> bool:
    """The paths ``RateLimitRequestsMiddleware`` budgets: all but the ones it lets through unbudgeted."""
    return path not in UNLIMITED_PATHS and not path.startswith(UNLIMITED_PREFIX)


def reference_shared_response(response_name: str) -> dict[str, str]:
    return {"$ref": f"#/components/responses/{response_name}"}


def dependant_calls(dependant: Dependant, *, dependency_call: Callable[..., Any]) -> bool:
    """Whether ``dependency_call`` is anywhere in the dependency tree below ``dependant``."""
    return any(
        sub_dependant.call is dependency_call or dependant_calls(sub_dependant, dependency_call=dependency_call)
        for sub_dependant in dependant.dependencies
    )


def find_optional_session_operations(application: FastAPI) -> frozenset[tuple[str, str]]:
    """The ``(path, method)`` of every route that depends on ``find_optional_account_access``: it works signed out
    and uses the session cookie only when one is live."""
    operations: set[tuple[str, str]] = set()
    for route_context in iter_route_contexts(application.routes):
        route = route_context.original_route
        path_format = route_context.path_format
        if (
            path_format is not None
            and isinstance(route, APIRoute)
            and dependant_calls(route.dependant, dependency_call=find_optional_account_access)
        ):
            operations.update((path_format, method.lower()) for method in route_context.methods or ())
    return frozenset(operations)


def document_operation_errors(
    operation: dict[str, Any], *, path: str, method: str, optional_session_operations: frozenset[tuple[str, str]]
) -> None:
    """Replaces the generated 422 with the real 400 and adds the refusals the auth and the rate limiter send."""
    responses = operation.setdefault("responses", {})
    if responses.pop("422", None) is not None:
        responses.setdefault("400", reference_shared_response(VALIDATION_FAILED_RESPONSE_NAME))
    security_requirements = list(operation.get("security", []))
    if security_requirements:
        # One route may read the cookie through two dependencies; FastAPI then lists its requirement twice.
        security_requirements = [
            requirement
            for index, requirement in enumerate(security_requirements)
            if requirement not in security_requirements[:index]
        ]
        if (path, method) in optional_session_operations:
            # An empty requirement object: no credentials needed (OpenAPI 3.1, Security Requirement Object). Signed
            # out is fine here, and a dead session counts as signed out: never a 401.
            security_requirements.append({})
        else:
            responses.setdefault("401", reference_shared_response(UNAUTHORIZED_RESPONSE_NAME))
        if method not in SAFE_METHODS:
            responses.setdefault("403", reference_shared_response(FORBIDDEN_RESPONSE_NAME))
        operation["security"] = security_requirements
    if is_rate_limited_path(path):
        # The limiter answers any such route with ``rate_limited``; a route that lists its own 429 codes gets it added.
        own_rate_limited_response = responses.get("429")
        if own_rate_limited_response is None:
            responses["429"] = reference_shared_response(RATE_LIMITED_RESPONSE_NAME)
        elif "`rate_limited`" not in own_rate_limited_response.get("description", "`rate_limited`"):
            own_rate_limited_response["description"] = (
                own_rate_limited_response["description"].removesuffix(".") + ", `rate_limited`."
            )


def replace_event_stream_item_schemas_with_text(operation: dict[str, Any]) -> None:
    """FastAPI describes a ``text/event-stream`` response's events with ``itemSchema``, a keyword OpenAPI 3.2 added;
    this document says 3.1, where the keyword is invalid (https://spec.openapis.org/oas/v3.2.0.html#media-type-object).
    In 3.1 the stream is a string; what its events carry is in the route's response description.
    """
    for response in operation.get("responses", {}).values():
        event_stream_content = response.get("content", {}).get("text/event-stream")
        if event_stream_content is not None and event_stream_content.pop("itemSchema", None) is not None:
            event_stream_content["schema"] = {"type": "string"}


def install_openapi_contract(application: FastAPI) -> None:
    """Wraps ``application.openapi`` (FastAPI's documented override point,
    https://fastapi.tiangolo.com/how-to/extending-openapi/) so the generated document carries the real error
    contract. The result is cached on the application like FastAPI's own."""
    generate_default_openapi = application.openapi

    def generate_contract_openapi() -> dict[str, Any]:
        if application.openapi_schema is not None:
            return application.openapi_schema
        openapi_schema = generate_default_openapi()
        components = openapi_schema.setdefault("components", {})
        component_schemas = components.setdefault("schemas", {})
        component_schemas.pop("HTTPValidationError", None)
        component_schemas.pop("ValidationError", None)
        error_response_schema = ErrorResponse.model_json_schema(ref_template="#/components/schemas/{model}")
        component_schemas.update(error_response_schema.pop("$defs", {}))
        component_schemas[ERROR_RESPONSE_SCHEMA_NAME] = error_response_schema
        components.setdefault("responses", {}).update(SHARED_ERROR_RESPONSES)
        components.setdefault("headers", {})[RETRY_AFTER_HEADER_NAME] = RETRY_AFTER_HEADER
        optional_session_operations = find_optional_session_operations(application)
        for path, path_item in openapi_schema.get("paths", {}).items():
            for method, operation in path_item.items():
                document_operation_errors(
                    operation, path=path, method=method, optional_session_operations=optional_session_operations
                )
                replace_event_stream_item_schemas_with_text(operation)
        strip_schema_titles(openapi_schema)
        application.openapi_schema = openapi_schema
        return openapi_schema

    application.openapi = generate_contract_openapi  # ty: ignore[invalid-assignment]  # FastAPI's override point
