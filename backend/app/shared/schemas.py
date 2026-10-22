"""The bases of every request and response body: snake_case on the wire (PLAN.md section 7.1)."""

import re
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints


class ApiRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ApiResponse(BaseModel):
    # A field with a default is still always sent, so the response schema lists it as required
    # (https://docs.pydantic.dev/latest/api/config/#pydantic.config.ConfigDict.json_schema_serialization_defaults_required).
    model_config = ConfigDict(frozen=True, json_schema_serialization_defaults_required=True)


class ErrorResponse(ApiResponse):
    """The body of every error answer: a stable ``code`` to branch on and a message that is safe to show."""

    code: str
    message: str


def require_web_page_url(value: str) -> str:
    """An http(s) URL with a host: a page's link to it can never run script (``javascript:``, ``data:``)."""
    parts = urlsplit(value)
    if parts.scheme not in {"https", "http"} or not parts.hostname:
        raise ValueError("must be an https:// or http:// URL with a host")
    return value


# A URL the pages link to, such as an invoice's return_url (2026-10-09: it took javascript: before).
WebPageUrl = Annotated[str, Field(max_length=2048), AfterValidator(require_web_page_url)]


# Unicode's Cc characters: C0, DEL and C1. PostgreSQL text cannot hold NUL
# (https://www.postgresql.org/docs/current/datatype-character.html), and the others have no business in a label.
CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# The same, except tab, line feed and carriage return.
MULTILINE_CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
# No whitespace at either end, so an identifier is stored and looked up exactly as sent.
IDENTIFIER_PATTERN = r"^\S(?:.*\S)?$"


def refuse_control_characters(value: str) -> str:
    if CONTROL_CHARACTER_PATTERN.search(value):
        raise ValueError("must not contain control characters")
    return value


def refuse_control_characters_except_tab_line_feed_and_carriage_return(value: str) -> str:
    if MULTILINE_CONTROL_CHARACTER_PATTERN.search(value):
        raise ValueError("must not contain control characters other than tabs and line breaks")
    return value


# Free text a person typed, trimmed: one line, or several. Length limits stay on each field.
SingleLineText = Annotated[str, StringConstraints(strip_whitespace=True), AfterValidator(refuse_control_characters)]
MultilineText = Annotated[
    str,
    StringConstraints(strip_whitespace=True),
    AfterValidator(refuse_control_characters_except_tab_line_feed_and_carriage_return),
]
# A caller's own identifier, such as an order id: never trimmed (a lookup matches it exactly), so whitespace at either
# end is refused instead, and so are control characters.
IdentifierText = Annotated[str, Field(pattern=IDENTIFIER_PATTERN), AfterValidator(refuse_control_characters)]
