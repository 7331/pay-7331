"""The request and response layer both clients share: request bodies and query strings built from keyword arguments,
the client settings, and the mapping of an answer to a model or a ``PayApiError``. The clients only send.

Bodies leave out every argument left at None, so the API applies its own default (PLAN.md section 7); booleans that
default to false are sent only when true. Amounts go out as decimal strings: a JSON float could round them."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from importlib.metadata import PackageNotFoundError, version
import json
import re
from typing import TypeAlias, TypeVar
from urllib.parse import quote

import httpx
from pydantic import BaseModel, JsonValue, ValidationError

from pay_7331.errors import (
    PayApiError,
    PayResponseError,
    PayTransportError,
    api_error_class_for_status,
    parse_retry_after_seconds,
)
from pay_7331.models import Chain, ExchangeRate, PaymentLink, PayModel

PRODUCTION_BASE_URL = "https://pay.7331.org"
SANDBOX_BASE_URL = "https://pay-sandbox.7331.org"
API_PATH_PREFIX = "/api/v1"
# httpx's own default is 5 s of inactivity (https://www.python-httpx.org/advanced/timeouts/); creating an invoice prices
# every offered network over public RPC endpoints, which can take longer, so the docs' examples use 30 s too.
DEFAULT_TIMEOUT_SECONDS = 30.0
MAXIMUM_ERROR_MESSAGE_CHARACTERS = 300
# The backend's API_TOKEN_PATTERN (backend/app/payments/accounts/api_token_service.py): the prefix and 43 to 128
# characters of unpadded base64url. Checked before the token goes into a header, so a token with a stray newline or
# space fails here, without echoing it, and never reaches an httpx error message.
API_TOKEN_PATTERN = re.compile(r"pay_tok_[A-Za-z0-9_-]{43,128}")
REDACTED_API_TOKEN = "pay_tok_[redacted]"  # noqa: S105  # the placeholder, not a secret

try:
    SDK_VERSION = version("pay-7331")
except PackageNotFoundError:
    SDK_VERSION = "0"

ModelType = TypeVar("ModelType", bound=BaseModel)
AmountValue: TypeAlias = str | Decimal | int


@dataclass(frozen=True)
class ClientSettings:
    """What both clients hand to their ``httpx`` client."""

    base_url: str
    # Holds the token: kept out of the repr, so a logged settings object shows none.
    headers: dict[str, str] = field(repr=False)
    timeout: httpx.Timeout
    api_token: str = field(repr=False)


def build_client_settings(*, api_token: str, base_url: str, timeout: float) -> ClientSettings:
    """``ValueError`` for a token that is not ``pay_tok_`` and 43 to 128 base64url characters; the message never
    holds the value (a near-miss token is still a secret)."""
    if not isinstance(api_token, str) or not API_TOKEN_PATTERN.fullmatch(api_token):
        raise ValueError(
            "api_token is not a pay_tok_... token: pass the token made in Settings exactly, with no spaces or newlines"
        )
    return ClientSettings(
        api_token=api_token,
        base_url=base_url.rstrip("/") + API_PATH_PREFIX,
        headers={
            "Authorization": f"Bearer {api_token}",
            "Accept": "application/json",
            "User-Agent": f"pay-7331-python/{SDK_VERSION}",
        },
        timeout=httpx.Timeout(timeout),
    )


def build_resource_path(collection: str, *, resource_id: str, action: str | None = None) -> str:
    """``/invoices/inv_...`` (and ``/cancel``): the id quoted, so no id can reach another path."""
    path = f"{collection}/{quote(resource_id, safe='')}"
    return f"{path}/{action}" if action is not None else path


def format_amount(amount: AmountValue) -> str:
    """A decimal string the API takes (``^[0-9]+(\\.[0-9]+)?$``). A float is refused: it would not be exact."""
    if isinstance(amount, bool) or not isinstance(amount, (str, Decimal, int)):
        raise TypeError("amounts are str, Decimal or int, never float")
    if isinstance(amount, Decimal):
        return format(amount, "f")
    return str(amount)


def format_datetime(moment: datetime) -> str:
    """ISO 8601; a naive time is read as UTC by the API."""
    return moment.isoformat()


def drop_unset(fields: Mapping[str, object]) -> dict[str, object]:
    return {name: value for name, value in fields.items() if value is not None and value is not False}


def build_invoice_body(
    *,
    amount: AmountValue,
    currency: str,
    chain: str | None,
    chains: Sequence[str] | None,
    assets: Sequence[str] | None,
    assets_by_chain: Mapping[str, Sequence[str]] | None,
    destination_address: str | None,
    allow_contract_destination: bool,
    note: str | None,
    order_id: str | None,
    refresh_expired: bool,
    lifetime_seconds: int | None,
    accuracy_percent: AmountValue | None,
    underpaid_open_until: datetime | None,
    return_url: str | None,
    success_url: str | None,
    callback_url: str | None,
    metadata: Mapping[str, JsonValue] | None,
) -> dict[str, object]:
    return drop_unset(
        {
            "amount": format_amount(amount),
            "currency": currency,
            "chain": chain,
            "chains": list(chains) if chains is not None else None,
            "assets": list(assets) if assets is not None else None,
            "assets_by_chain": {key: list(symbols) for key, symbols in assets_by_chain.items()}
            if assets_by_chain is not None
            else None,
            "destination_address": destination_address,
            "allow_contract_destination": allow_contract_destination,
            "note": note,
            "order_id": order_id,
            "refresh_expired": refresh_expired,
            "lifetime_seconds": lifetime_seconds,
            "accuracy_percent": format_amount(accuracy_percent) if accuracy_percent is not None else None,
            "underpaid_open_until": format_datetime(underpaid_open_until) if underpaid_open_until is not None else None,
            "return_url": return_url,
            "success_url": success_url,
            "callback_url": callback_url,
            "metadata": dict(metadata) if metadata is not None else None,
        }
    )


def build_payment_link_body(
    *,
    amount: AmountValue,
    currency: str,
    quantity: int,
    chain: str | None,
    chains: Sequence[str] | None,
    assets: Sequence[str] | None,
    assets_by_chain: Mapping[str, Sequence[str]] | None,
    destination_address: str | None,
    allow_contract_destination: bool,
    note: str | None,
    lifetime_seconds: int | None,
    accuracy_percent: AmountValue | None,
    return_url: str | None,
) -> dict[str, object]:
    return drop_unset(
        {
            "amount": format_amount(amount),
            "currency": currency,
            "quantity": quantity,
            "chain": chain,
            "chains": list(chains) if chains is not None else None,
            "assets": list(assets) if assets is not None else None,
            "assets_by_chain": {key: list(symbols) for key, symbols in assets_by_chain.items()}
            if assets_by_chain is not None
            else None,
            "destination_address": destination_address,
            "allow_contract_destination": allow_contract_destination,
            "note": note,
            "lifetime_seconds": lifetime_seconds,
            "accuracy_percent": format_amount(accuracy_percent) if accuracy_percent is not None else None,
            "return_url": return_url,
        }
    )


def build_static_deposit_address_body(
    *,
    customer_reference: str,
    chain: str | None,
    assets: Sequence[str] | None,
    allow_contract_destination: bool,
) -> dict[str, object]:
    return drop_unset(
        {
            "customer_reference": customer_reference,
            "chain": chain,
            "assets": list(assets) if assets is not None else None,
            "allow_contract_destination": allow_contract_destination,
        }
    )


def build_query(**parameters: str | int | datetime | None) -> dict[str, str | int]:
    return {
        name: format_datetime(value) if isinstance(value, datetime) else value
        for name, value in parameters.items()
        if value is not None
    }


def build_api_error(response: httpx.Response) -> PayApiError:
    """The ``PayApiError`` subclass of the status, from the API's ``{"code", "message"}`` or, when the body is not
    that (a proxy's page), the body's start. The body must have been read."""
    code = "http_error"
    message = response.text[:MAXIMUM_ERROR_MESSAGE_CHARACTERS] or response.reason_phrase
    try:
        body = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        body = None
    if isinstance(body, dict) and isinstance(body.get("code"), str):
        code = body["code"]
        message = str(body.get("message", code))
    error_class = api_error_class_for_status(response.status_code)
    return error_class(
        status_code=response.status_code,
        code=code,
        message=message,
        retry_after=parse_retry_after_seconds(response.headers.get("retry-after")),
    )


def parse_response(response: httpx.Response, *, model: type[ModelType]) -> ModelType:
    if not response.is_success:
        raise build_api_error(response)
    try:
        return model.model_validate_json(response.content)
    except ValidationError as error:
        raise PayResponseError(
            f"{response.request.method} {response.request.url.path}: the {response.status_code} answer does not parse "
            f"as {model.__name__} ({error.error_count()} fields failed)"
        ) from error


def build_transport_error(error: httpx.RequestError, *, request_description: str, api_token: str) -> PayTransportError:
    """A ``PayTransportError`` naming the httpx error, with the token redacted wherever its message quotes it. Raise it
    outside the ``except`` block that caught ``error``: neither ``__cause__`` nor ``__context__`` may hold the httpx
    error, whose message or request can carry the ``Authorization`` header."""
    error_text = str(error).replace(api_token, REDACTED_API_TOKEN)[:MAXIMUM_ERROR_MESSAGE_CHARACTERS]
    return PayTransportError(f"{request_description}: {type(error).__name__}: {error_text}")


class ChainListResponse(PayModel):
    chains: list[Chain]


class RateListResponse(PayModel):
    rates: list[ExchangeRate]


class PaymentLinkListResponse(PayModel):
    payment_links: list[PaymentLink]
