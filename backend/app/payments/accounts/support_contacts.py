"""The support contact an account shows its payers: an email address, a Telegram handle or an https
link, saved as typed and linked on payer pages (PLAN.md section 7)."""

import re
from urllib.parse import quote, urlsplit

from app.payments.accounts.types import SellerSupportContact
from app.shared.errors import InvalidInputError

SUPPORT_CONTACT_MAXIMUM_LENGTH = 200
# A Telegram username: 5 to 32 of a-z, 0-9 and _.
TELEGRAM_HANDLE_PATTERN = re.compile(r"@([A-Za-z0-9_]{5,32})")
# HTML's "valid email address" (https://html.spec.whatwg.org/multipage/input.html#valid-e-mail-address), with at least
# one dot in the domain.
EMAIL_ADDRESS_PATTERN = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+"
)
SUPPORT_CONTACT_REFUSAL = (
    "support_contact: an email address, a Telegram handle like @name (5 to 32 letters, digits or _), "
    "or an https:// link"
)


def parse_https_support_link(support_contact: str) -> SellerSupportContact | None:
    """An https link with a dotted host and no user info, spaces or control characters; else None."""
    if not support_contact.startswith("https://") or any(
        character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F for character in support_contact
    ):
        return None
    try:
        parts = urlsplit(support_contact)
        hostname = parts.hostname
    except ValueError:
        return None
    if parts.scheme != "https" or not hostname or "." not in hostname or parts.username or parts.password:
        return None
    return SellerSupportContact(
        kind="url", display_text=support_contact.removeprefix("https://").rstrip("/"), href=support_contact
    )


def parse_support_contact(support_contact: str) -> SellerSupportContact | None:
    """How payer pages show and link a saved support contact; None when it is none of the three kinds."""
    if telegram_match := TELEGRAM_HANDLE_PATTERN.fullmatch(support_contact):
        telegram_username = telegram_match.group(1)
        return SellerSupportContact(
            kind="telegram", display_text=f"@{telegram_username}", href=f"https://t.me/{telegram_username}"
        )
    if EMAIL_ADDRESS_PATTERN.fullmatch(support_contact):
        # Every character but @ outside the URL-unreserved set is percent-encoded (RFC 6068), so `?` or `&` in a local
        # part can never add headers to the mailto: link.
        return SellerSupportContact(
            kind="email", display_text=support_contact, href=f"mailto:{quote(support_contact, safe='@')}"
        )
    return parse_https_support_link(support_contact)


def normalize_support_contact(support_contact: str | None) -> str | None:
    """The contact to save: trimmed, None for empty; refused unless it is one of the three kinds."""
    trimmed_support_contact = (support_contact or "").strip()
    if not trimmed_support_contact:
        return None
    if (
        len(trimmed_support_contact) > SUPPORT_CONTACT_MAXIMUM_LENGTH
        or parse_support_contact(trimmed_support_contact) is None
    ):
        raise InvalidInputError(SUPPORT_CONTACT_REFUSAL)
    return trimmed_support_contact
