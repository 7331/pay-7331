"""Request budgets (PLAN.md section 9), GCRA in Redis.

Per client network (the salted hash of an IPv4 address or an IPv6 /64, app/shared/request.py): 120 requests per minute
on every route but health and ``/static/`` (a page load fetches about a dozen assets, 2026-10-09), and 10 per minute on
public writes (``POST /api/v1/public/...``, invoice creation included, the account's ``POST /api/v1/invoices``,
``POST /api/v1/payment-links``, ``POST /api/v1/static-deposit-addresses`` and ``POST /api/v1/invoice-schedules``, and
the sign-up, sign-in, add-passkey, delete-passkey, passkey-enrollment-link, payout-address-change, API-token and
webhook passkey steps). From IPv6, public writes are also counted over the address's whole /48 together:
``PAY_PUBLIC_WRITES_PER_MINUTE_PER_IPV6_48`` (default 60), since one holder of a /48 has 65,536 /64s. IPv4 has no such
bucket.

Per account for a valid API token on the routes it opens (invoices 2026-10-12; the CSV export, cancel,
accept-partial, quantity links and the event list 2026-10-21; static deposit addresses, PLAN.md section 17; invoice
schedules, section 18): an account's server sits on one IP, so its budgets follow the account, not the network, and
the per-network public-write budget does not apply. 120 requests per minute per account, and
``PAY_API_TOKEN_INVOICE_CREATES_PER_MINUTE`` (default 60) on the creations together: ``POST /api/v1/invoices``, ``POST
/api/v1/payment-links``, ``POST /api/v1/static-deposit-addresses`` and ``POST /api/v1/invoice-schedules``. A
token is valid when its SHA-256 names a row (one indexed read); an invalid one is budgeted like any request and the
route answers 401.

The CSV export (``GET /api/v1/invoices/export.csv``, 2026-10-21) reads every invoice of the account, so it has a budget
of its own on top: ``INVOICE_EXPORTS_PER_WINDOW`` a minute, per account for a token and per network otherwise.

Fail open: the budget is an abuse backstop, and a Redis outage must not take the API down with it. A token lookup that
fails (Postgres down) leaves the request on the per-network budgets.
"""

import re

from sqlalchemy.exc import SQLAlchemyError
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from app.infrastructure.rate_limit.limiter import RateLimiter
from app.lifespan.dependencies import resources_of
from app.lifespan.resources import ApiResources
from app.payments.accounts.api_token_service import AccountApiTokenService, read_bearer_api_token
from app.shared.client_network import hash_client_ipv6_48_network, hash_client_network
from app.shared.errors import RateLimitedError
from app.shared.exception_handlers import app_error_response
from app.shared.request import read_client_ip_address

REQUESTS_PER_WINDOW_PER_IP = 120
PUBLIC_WRITES_PER_WINDOW_PER_IP = 10
REQUESTS_PER_WINDOW_PER_API_TOKEN_ACCOUNT = 120
WINDOW_SECONDS = 60
UNLIMITED_PATHS = frozenset({"/api/v1/health"})
UNLIMITED_PREFIX = "/static/"
PUBLIC_WRITE_PREFIX = "/api/v1/public/"
ACCOUNT_INVOICES_PATH = "/api/v1/invoices"
INVOICE_EXPORT_PATH = "/api/v1/invoices/export.csv"
INVOICE_EXPORTS_PER_WINDOW = 6
ACCOUNT_PAYMENT_LINKS_PATH = "/api/v1/payment-links"
STATIC_DEPOSIT_ADDRESSES_PATH = "/api/v1/static-deposit-addresses"
ACCOUNT_INVOICE_SCHEDULES_PATH = "/api/v1/invoice-schedules"
# PUT too: the payout address change and setting the webhook are PUTs (before 2026-10-12 only POST counted, so the
# payout change's PUT was never budgeted as a write).
PUBLIC_WRITE_METHODS = frozenset({"POST", "PUT"})
# Writes outside /public/ counted the same way: sign-up and sign-in need no session and each stores a challenge or
# tries a credential; adding or deleting a passkey, changing the payout address, making an API token and changing the webhook or
# the Discord webhook store one too (and a test message posts to someone else's server); an account's invoice costs what a public one does.
PUBLIC_WRITE_PATHS = frozenset(
    {
        "/api/v1/accounts/registration-options",
        "/api/v1/accounts",
        "/api/v1/sessions/authentication-options",
        "/api/v1/sessions",
        "/api/v1/account-passkeys/addition-options",
        "/api/v1/account-passkeys/registration-options",
        "/api/v1/account-passkeys",
        "/api/v1/accounts/current/payout-address-change-options",
        "/api/v1/accounts/current/payout-address",
        "/api/v1/account-api-tokens/creation-options",
        "/api/v1/account-api-tokens",
        "/api/v1/account-webhook/change-options",
        "/api/v1/account-webhook",
        "/api/v1/account-webhook/secret-rotation",
        "/api/v1/account-webhook/test-events",
        "/api/v1/account-discord-webhook/change-options",
        "/api/v1/account-discord-webhook",
        "/api/v1/account-discord-webhook/test-messages",
        # The Monero wallet change stores a challenge and asks the sidecar (2026-10-14).
        "/api/v1/account-monero-wallet/change-options",
        "/api/v1/account-monero-wallet",
        # A passkey enrollment link's steps need no session, like sign-up, and each tries a token (2026-10-15).
        "/api/v1/passkey-enrollment-links/registration-options",
        "/api/v1/passkey-enrollment-links/passkeys",
        ACCOUNT_INVOICES_PATH,
        ACCOUNT_PAYMENT_LINKS_PATH,
        # A static deposit address's creation checks its destination and asks the factory (PLAN.md section 17).
        STATIC_DEPOSIT_ADDRESSES_PATH,
        ACCOUNT_INVOICE_SCHEDULES_PATH,
    }
)

# The id-bearing paths below are matched by the route's shape, any one path segment where the id goes, never by the id's
# format: the route takes what its parameter coerces (pydantic's lax int takes `+5`, `5.0`, `5_0` and, URL-decoded,
# `%205`), so a format check here would let such a spelling reach the route uncounted.
# POST /api/v1/account-passkeys/{id}/deletion-options and /deletion: a fresh passkey check's two steps.
ACCOUNT_PASSKEY_DELETION_PATH_PATTERN = re.compile(r"/api/v1/account-passkeys/[^/]+/(deletion-options|deletion)")

ACCOUNT_EVENTS_PATH = "/api/v1/events"
# The creations an API token's per-account create budget counts.
API_TOKEN_CREATION_PATHS = frozenset(
    {ACCOUNT_INVOICES_PATH, ACCOUNT_PAYMENT_LINKS_PATH, STATIC_DEPOSIT_ADDRESSES_PATH, ACCOUNT_INVOICE_SCHEDULES_PATH}
)
# GET /api/v1/invoices/{invoice_id} (`/api/v1/invoices/export.csv` matches too; it is a token route as well).
ACCOUNT_INVOICE_PATH_PATTERN = re.compile(r"/api/v1/invoices/[^/]+")
# POST /api/v1/invoices/{invoice_id}/cancel and /accept-partial (2026-10-21).
ACCOUNT_INVOICE_ACTION_PATH_PATTERN = re.compile(r"/api/v1/invoices/[^/]+/(cancel|accept-partial)")
# GET /api/v1/payment-links/{id} and POST /api/v1/payment-links/{id}/close.
ACCOUNT_PAYMENT_LINK_PATH_PATTERN = re.compile(r"/api/v1/payment-links/[^/]+")
ACCOUNT_PAYMENT_LINK_CLOSE_PATH_PATTERN = re.compile(r"/api/v1/payment-links/[^/]+/close")
# GET /api/v1/static-deposit-addresses/{id} (PLAN.md section 17).
STATIC_DEPOSIT_ADDRESS_PATH_PATTERN = re.compile(r"/api/v1/static-deposit-addresses/[^/]+")
# GET /api/v1/invoice-schedules/{id} and its /invoices, and POST its /pause, /resume and /cancel (2026-10-21).
ACCOUNT_INVOICE_SCHEDULE_READ_PATH_PATTERN = re.compile(r"/api/v1/invoice-schedules/[^/]+(/invoices)?")
ACCOUNT_INVOICE_SCHEDULE_ACTION_PATH_PATTERN = re.compile(r"/api/v1/invoice-schedules/[^/]+/(pause|resume|cancel)")


def is_public_write_path(path: str) -> bool:
    return (
        path.startswith(PUBLIC_WRITE_PREFIX)
        or path in PUBLIC_WRITE_PATHS
        or ACCOUNT_PASSKEY_DELETION_PATH_PATTERN.fullmatch(path) is not None
    )


def is_api_token_route(method: str, *, path: str) -> bool:
    """The routes an API token opens (the ones taking ``AccountOrApiTokenAccessDependency``): ``POST`` and ``GET`` of
    ``/api/v1/invoices``, ``/api/v1/payment-links``, ``/api/v1/static-deposit-addresses`` and
    ``/api/v1/invoice-schedules``, ``GET`` of one invoice, one link, one static deposit address, one schedule and its
    invoices, the CSV export and ``/api/v1/events``, and ``POST`` of an invoice's cancel and accept-partial, a link's
    close and a schedule's pause, resume and cancel."""
    if path in API_TOKEN_CREATION_PATHS:
        return method in {"GET", "POST"}
    if method == "GET":
        return (
            path in {INVOICE_EXPORT_PATH, ACCOUNT_EVENTS_PATH}
            or ACCOUNT_INVOICE_PATH_PATTERN.fullmatch(path) is not None
            or ACCOUNT_PAYMENT_LINK_PATH_PATTERN.fullmatch(path) is not None
            or STATIC_DEPOSIT_ADDRESS_PATH_PATTERN.fullmatch(path) is not None
            or ACCOUNT_INVOICE_SCHEDULE_READ_PATH_PATTERN.fullmatch(path) is not None
        )
    return method == "POST" and (
        ACCOUNT_INVOICE_ACTION_PATH_PATTERN.fullmatch(path) is not None
        or ACCOUNT_PAYMENT_LINK_CLOSE_PATH_PATTERN.fullmatch(path) is not None
        or ACCOUNT_INVOICE_SCHEDULE_ACTION_PATH_PATTERN.fullmatch(path) is not None
    )


async def find_api_token_account_id(request: Request, *, resources: ApiResources) -> int | None:
    """The account of a valid bearer token on an API token route, else None (no token, another route, a bad token, or
    Postgres unreachable)."""
    if not is_api_token_route(request.method, path=request.url.path):
        return None
    api_token = read_bearer_api_token(request.headers.get("authorization"))
    if api_token is None:
        return None
    try:
        async with resources.database.session_factory() as session, session.begin():
            return await AccountApiTokenService.on_session(session).find_api_token_account_id(api_token)
    except (SQLAlchemyError, OSError):
        return None


async def seconds_until_public_write_admitted(
    rate_limiter: RateLimiter,
    *,
    client_hash: str,
    client_ipv6_48_hash: str | None,
    public_writes_per_minute_per_ipv6_48: int,
) -> int:
    """Spends one public write of the client network's budget and, from IPv6, one of its /48's."""
    retry_after_seconds = await rate_limiter.seconds_until_admitted(
        f"public_write:{client_hash}", limit=PUBLIC_WRITES_PER_WINDOW_PER_IP, window_seconds=WINDOW_SECONDS
    )
    if retry_after_seconds == 0 and client_ipv6_48_hash is not None:
        retry_after_seconds = await rate_limiter.seconds_until_admitted(
            f"public_write_ipv6_48:{client_ipv6_48_hash}",
            limit=public_writes_per_minute_per_ipv6_48,
            window_seconds=WINDOW_SECONDS,
        )
    return retry_after_seconds


class RateLimitRequestsMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:  # noqa: PLR0917  # ASGI interface
        if scope["type"] != "http" or scope["path"] in UNLIMITED_PATHS or scope["path"].startswith(UNLIMITED_PREFIX):
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        resources = resources_of(scope)
        rate_limiter = resources.rate_limiter
        api_token_account_id = await find_api_token_account_id(request, resources=resources)
        if api_token_account_id is not None:
            retry_after_seconds = await rate_limiter.seconds_until_admitted(
                f"api_token_account:{api_token_account_id}",
                limit=REQUESTS_PER_WINDOW_PER_API_TOKEN_ACCOUNT,
                window_seconds=WINDOW_SECONDS,
            )
            if retry_after_seconds == 0 and request.method == "POST" and scope["path"] in API_TOKEN_CREATION_PATHS:
                retry_after_seconds = await rate_limiter.seconds_until_admitted(
                    f"api_token_invoice_create:{api_token_account_id}",
                    limit=resources.invoice_wiring.invoice_settings.api_token_invoice_creates_per_minute,
                    window_seconds=WINDOW_SECONDS,
                )
            if retry_after_seconds == 0 and scope["path"] == INVOICE_EXPORT_PATH:
                retry_after_seconds = await rate_limiter.seconds_until_admitted(
                    f"invoice_export_account:{api_token_account_id}",
                    limit=INVOICE_EXPORTS_PER_WINDOW,
                    window_seconds=WINDOW_SECONDS,
                )
        else:
            client_ip_address = read_client_ip_address(
                request, trusted_proxy_addresses=resources.trusted_proxy_addresses
            )
            ip_hash_salt = resources.invoice_wiring.screening_settings.ip_hash_salt.get_secret_value()
            client_hash = hash_client_network(client_ip_address, salt=ip_hash_salt)
            retry_after_seconds = await rate_limiter.seconds_until_admitted(
                f"ip:{client_hash}", limit=REQUESTS_PER_WINDOW_PER_IP, window_seconds=WINDOW_SECONDS
            )
            if (
                retry_after_seconds == 0
                and request.method in PUBLIC_WRITE_METHODS
                and is_public_write_path(scope["path"])
            ):
                retry_after_seconds = await seconds_until_public_write_admitted(
                    rate_limiter,
                    client_hash=client_hash,
                    client_ipv6_48_hash=hash_client_ipv6_48_network(client_ip_address, salt=ip_hash_salt),
                    public_writes_per_minute_per_ipv6_48=(
                        resources.invoice_wiring.invoice_settings.public_writes_per_minute_per_ipv6_48
                    ),
                )
            if retry_after_seconds == 0 and scope["path"] == INVOICE_EXPORT_PATH:
                retry_after_seconds = await rate_limiter.seconds_until_admitted(
                    f"invoice_export:{client_hash}", limit=INVOICE_EXPORTS_PER_WINDOW, window_seconds=WINDOW_SECONDS
                )
        if retry_after_seconds > 0:
            refusal = app_error_response(
                RateLimitedError("too many requests; slow down", retry_after_seconds=retry_after_seconds)
            )
            await refusal(scope, receive, send)
            return
        await self.app(scope, receive, send)
