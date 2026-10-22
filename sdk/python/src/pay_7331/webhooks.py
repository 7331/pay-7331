"""Webhook verification, with the standard library only.

pay-7331 signs every delivery per Standard Webhooks
(https://github.com/standard-webhooks/standard-webhooks/blob/main/spec/standard-webhooks.md): the signed content is
``{webhook-id}.{webhook-timestamp}.{body}``, the key is the base64 after the ``whsec_`` prefix, and
``webhook-signature`` is a space-delimited list of ``v1,<base64 HMAC-SHA256>``, any one of which may match; the
comparison is constant-time (``hmac.compare_digest``) and the timestamp must be within a tolerance of now, against
replays. The spec names no tolerance; 5 minutes is what its reference libraries use and what the docs' own example
checks.

No ``standardwebhooks`` dependency: the check is a dozen lines of ``hmac``, the same as the docs' example, and a
merchant's verifier should not need a package to read its own payment news.
"""

import base64
import binascii
from collections.abc import Mapping
import hashlib
import hmac
import json
import re
import time

from pydantic import ValidationError

from pay_7331.errors import WebhookVerificationError
from pay_7331.models import WebhookEvent

WEBHOOK_SECRET_PREFIX = "whsec_"  # noqa: S105  # the format prefix, not a secret
DEFAULT_TIMESTAMP_TOLERANCE_SECONDS = 300
# Unix seconds in ASCII digits only (str.isdigit also takes "²" and other Unicode digits); 12 digits reach the year
# 33658, so int() never sees a huge string.
WEBHOOK_TIMESTAMP_PATTERN = re.compile(r"[0-9]{1,12}")


def decode_webhook_secret(secret: str) -> bytes:
    """The HMAC key: the base64 after ``whsec_``, padded or not (the Standard Webhooks reference library pads it too,
    https://github.com/standard-webhooks/standard-webhooks/blob/main/libraries/python/standardwebhooks/webhooks.py).
    A secret that is not that is the caller's mistake: ``ValueError``."""
    encoded_key = secret.removeprefix(WEBHOOK_SECRET_PREFIX)
    try:
        key = base64.b64decode(encoded_key + "=" * (-len(encoded_key) % 4), validate=True)
    except binascii.Error as error:
        raise ValueError("the webhook secret is not whsec_ followed by base64") from error
    if not key:
        raise ValueError("the webhook secret is empty")
    return key


def sign_webhook_content(*, key: bytes, webhook_id: str, timestamp: str, payload: bytes) -> bytes:
    signed_content = f"{webhook_id}.{timestamp}.".encode() + payload
    return hmac.new(key, signed_content, hashlib.sha256).digest()


def has_matching_signature(signature_header: str, *, expected_signature: bytes) -> bool:
    """Any ``v1,`` entry of the space-delimited header that decodes to the expected digest; other versions skipped."""
    matched = False
    for versioned_signature in signature_header.split():
        version, _, encoded_signature = versioned_signature.partition(",")
        if version != "v1":
            continue
        try:
            signature = base64.b64decode(encoded_signature, validate=True)
        except binascii.Error:
            continue
        # No early return: every entry is compared, so the time taken does not say which one matched.
        matched = hmac.compare_digest(signature, expected_signature) or matched
    return matched


def verify_webhook(
    payload: bytes | str,
    *,
    headers: Mapping[str, str],
    secret: str,
    tolerance_seconds: int = DEFAULT_TIMESTAMP_TOLERANCE_SECONDS,
) -> WebhookEvent:
    """Checks a delivery and returns its event; raises ``WebhookVerificationError`` otherwise.

    ``payload`` is the request body exactly as received (bytes, or a str the framework decoded, encoded back as UTF-8;
    re-serialised JSON would not match), ``headers`` the
    request headers (any mapping; names are matched case-insensitively), ``secret`` the ``whsec_...`` secret Settings
    showed. A delivery whose ``webhook-timestamp`` is more than ``tolerance_seconds`` from now is refused, so a
    captured delivery cannot be replayed later; keep the server's clock in sync (NTP).

    The event's ``webhook_id`` is the same on every retry: store it with the work it starts and skip ids seen before.
    ``is_test`` marks a "Send test" delivery."""
    key = decode_webhook_secret(secret)
    payload_bytes = payload.encode() if isinstance(payload, str) else payload
    lowercase_headers = {name.lower(): value for name, value in headers.items()}
    webhook_id = lowercase_headers.get("webhook-id")
    timestamp = lowercase_headers.get("webhook-timestamp")
    signature_header = lowercase_headers.get("webhook-signature")
    if not webhook_id or not timestamp or not signature_header:
        raise WebhookVerificationError("missing webhook-id, webhook-timestamp or webhook-signature header")
    if not WEBHOOK_TIMESTAMP_PATTERN.fullmatch(timestamp):
        raise WebhookVerificationError("webhook-timestamp is not unix seconds")
    if abs(time.time() - int(timestamp)) > tolerance_seconds:
        raise WebhookVerificationError("webhook-timestamp is outside the tolerance: too old or too new")
    expected_signature = sign_webhook_content(
        key=key, webhook_id=webhook_id, timestamp=timestamp, payload=payload_bytes
    )
    if not has_matching_signature(signature_header, expected_signature=expected_signature):
        raise WebhookVerificationError("no webhook-signature matches the secret")
    try:
        body = json.loads(payload_bytes)
        if not isinstance(body, dict):
            raise WebhookVerificationError("the signed body is not a JSON object")
        return WebhookEvent.model_validate(
            {
                "webhook_id": webhook_id,
                "type": body.get("type"),
                "timestamp": body.get("timestamp"),
                "data": body.get("data"),
                "is_test": body.get("test") is True,
            }
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as error:
        raise WebhookVerificationError("the signed body is not a pay-7331 event") from error
