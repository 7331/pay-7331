"""Typed log records of sign-up screening. Never a client IP; the refusal reason only."""

import logging

from app.shared.logging import LogEvent


class SignUpRefused(LogEvent):
    message = "Sign-up screening refused a sign-up"
    operation = "sign_up_screening"
    reason: str


class SignUpScreeningLookupFailed(LogEvent):
    level = logging.WARNING
    message = "ip-api lookup failed during sign-up screening"
    operation = "sign_up_screening"
    fail_closed: bool
    # An ``IpApiFailureKind`` value, or ``no_ip_api_key`` when PAY_IP_API_KEY is empty.
    reason: str
    # ip-api's own fixed text of a ``fail`` record, key and address redacted (``IpApiError``); never the IP.
    ip_api_message: str | None = None


class IpApiKeyRejectionNotMarked(LogEvent):
    level = logging.WARNING
    message = "ip-api rejected the screening key, and Redis did not take the marker the operator alert reads"
    operation = "sign_up_screening"


class ScreeningCacheUnavailable(LogEvent):
    level = logging.WARNING
    message = "The screening verdict cache in Redis is unavailable"
    operation = "sign_up_screening"
