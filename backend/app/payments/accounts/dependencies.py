"""``AccountAccessDependency``: the route needs a signed-in account, by passkey session cookie (PLAN.md section 7).
``OptionalAccountAccessDependency``: the signed-in account if the cookie holds a live session, else None (public
invoice creation). ``AccountSessionTokenDependency``: the cookie's token, if any. ``AccountServiceDependency``: the
request-scoped AccountService. ``AccountPreferencesServiceDependency``: the account's saved coins, confirmation counts
and support contact. ``PasskeyEnrollmentLinkServiceDependency``: the enrollment link service (2026-10-15).
``AccountPasskeyServiceDependency``: the signed-in account's own passkeys.
``AccountOrApiTokenAccessDependency``: a bearer API token or else the cookie, on the
routes an account's own server calls (invoices 2026-10-12; the CSV export, cancel, accept-partial, quantity links
and the event list 2026-10-21; static deposit addresses, PLAN.md section 17; app/rate_limit_middleware.py lists the
same routes)."""

from typing import Annotated

from fastapi import Depends, Request, Security
from fastapi.security import APIKeyCookie, HTTPAuthorizationCredentials, HTTPBearer
from pydantic import SecretStr

from app.lifespan.dependencies import DatabaseSession
from app.payments.accounts.account_passkey_service import AccountPasskeyService
from app.payments.accounts.api_token_service import AccountApiTokenService, read_bearer_api_token
from app.payments.accounts.errors import BearerChallengedUnauthorizedError, CookieChallengedUnauthorizedError
from app.payments.accounts.passkey_enrollment_link_service import PasskeyEnrollmentLinkService
from app.payments.accounts.preferences_service import AccountPreferencesService
from app.payments.accounts.service import AccountService
from app.payments.accounts.types import ACCOUNT_SESSION_COOKIE_NAME, AccountAccess, AccountApiTokenAccess
from app.shared.errors import UnauthorizedError

# Both read without refusing (auto_error=False): the dependencies below decide, with the codes they always sent. Declaring them as security schemes puts them in the OpenAPI document
# (https://fastapi.tiangolo.com/reference/security/).
account_session_cookie_scheme = APIKeyCookie(
    name=ACCOUNT_SESSION_COOKIE_NAME,
    scheme_name="accountSession",
    description="The session cookie a passkey sign-in sets. A change also needs Origin to be this site's own.",
    auto_error=False,
)
api_token_bearer_scheme = HTTPBearer(
    scheme_name="apiToken",
    description="An account's API token (`pay_tok_...`), made in settings, for the account's own server.",
    auto_error=False,
)


def get_account_service(session: DatabaseSession) -> AccountService:
    return AccountService.on_session(session)


AccountServiceDependency = Annotated[AccountService, Depends(get_account_service)]


def get_account_preferences_service(session: DatabaseSession) -> AccountPreferencesService:
    return AccountPreferencesService.on_session(session)


AccountPreferencesServiceDependency = Annotated[AccountPreferencesService, Depends(get_account_preferences_service)]


def get_passkey_enrollment_link_service(session: DatabaseSession) -> PasskeyEnrollmentLinkService:
    return PasskeyEnrollmentLinkService.on_session(session)


PasskeyEnrollmentLinkServiceDependency = Annotated[
    PasskeyEnrollmentLinkService, Depends(get_passkey_enrollment_link_service)
]


def get_account_passkey_service(session: DatabaseSession) -> AccountPasskeyService:
    return AccountPasskeyService.on_session(session)


AccountPasskeyServiceDependency = Annotated[AccountPasskeyService, Depends(get_account_passkey_service)]


def read_account_session_token(
    session_cookie: Annotated[str | None, Security(account_session_cookie_scheme)],
) -> SecretStr | None:
    return SecretStr(session_cookie) if session_cookie else None


AccountSessionTokenDependency = Annotated[SecretStr | None, Depends(read_account_session_token)]


async def require_account_access(
    request: Request,
    *,
    session_token: AccountSessionTokenDependency,
    account_service: AccountServiceDependency,
) -> AccountAccess:
    """The session cookie's account; a refusal names the cookie in ``WWW-Authenticate``."""
    try:
        return await account_service.authenticate_account(
            session_token, request_method=request.method, origin_header=request.headers.get("origin")
        )
    except UnauthorizedError as session_refusal:
        raise CookieChallengedUnauthorizedError(session_refusal) from session_refusal


AccountAccessDependency = Annotated[AccountAccess, Depends(require_account_access)]


async def find_optional_account_access(
    request: Request,
    *,
    session_token: AccountSessionTokenDependency,
    account_service: AccountServiceDependency,
) -> AccountAccess | None:
    return await account_service.find_account_access(
        session_token, request_method=request.method, origin_header=request.headers.get("origin")
    )


OptionalAccountAccessDependency = Annotated[AccountAccess | None, Depends(find_optional_account_access)]


def get_account_api_token_service(session: DatabaseSession) -> AccountApiTokenService:
    return AccountApiTokenService.on_session(session)


AccountApiTokenServiceDependency = Annotated[AccountApiTokenService, Depends(get_account_api_token_service)]


async def require_account_or_api_token_access(
    request: Request,
    *,
    session_token: AccountSessionTokenDependency,
    account_service: AccountServiceDependency,
    account_api_token_service: AccountApiTokenServiceDependency,
    bearer_credentials: Annotated[HTTPAuthorizationCredentials | None, Security(api_token_bearer_scheme)],
) -> AccountAccess | AccountApiTokenAccess:
    """The routes an account's server may call: ``Authorization: Bearer pay_tok_...``
    when sent (then the token alone decides, cookie or not), else the session cookie as on every account route. A
    cookie refusal here also says ``WWW-Authenticate: Bearer``, since these routes take a bearer token."""
    if bearer_credentials is not None:
        api_token: SecretStr | None = SecretStr(bearer_credentials.credentials)
    else:
        # HTTPBearer returns None for a bare "Bearer" with no token too; that is still a bearer attempt, refused as
        # 401 api_token_invalid, never handed on to the cookie.
        api_token = read_bearer_api_token(request.headers.get("authorization"))
    if api_token is not None:
        return await account_api_token_service.authenticate_api_token(api_token)
    try:
        return await account_service.authenticate_account(
            session_token, request_method=request.method, origin_header=request.headers.get("origin")
        )
    except UnauthorizedError as cookie_refusal:
        raise BearerChallengedUnauthorizedError(cookie_refusal) from cookie_refusal


AccountOrApiTokenAccessDependency = Annotated[
    AccountAccess | AccountApiTokenAccess, Depends(require_account_or_api_token_access)
]
