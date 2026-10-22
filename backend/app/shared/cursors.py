"""The base64url text inside the list cursors this API hands out (the invoice list's and the account events')."""

import base64
import re

# Only the base64url alphabet and its padding: Python 3.15 drops (with a FutureWarning, an untyped stderr line) the
# characters ``urlsafe_b64decode`` used to refuse, so anything else is refused here first.
URL_SAFE_BASE64_TEXT_PATTERN = re.compile(r"[A-Za-z0-9_-]+=*")


def decode_url_safe_base64_cursor(cursor: str) -> str:
    """The cursor's text; ``ValueError`` (``binascii.Error`` is one) for anything this API did not return."""
    if URL_SAFE_BASE64_TEXT_PATTERN.fullmatch(cursor) is None:
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(cursor.encode()).decode()
