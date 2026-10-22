"""Account, passkey and session refusals callers tell apart."""

from app.payments.accounts.types import ACCOUNT_SESSION_COOKIE_NAME
from app.shared.errors import AppError, ConflictError, ForbiddenError, NotFoundError, UnauthorizedError


class PasskeyResponseInvalidError(AppError):
    """The browser's credential did not verify: wrong origin or RP id, bad signature, no user verification, ..."""

    code = "passkey_response_invalid"
    status = 400


class PasskeyChallengeExpiredError(AppError):
    """No unused, unexpired challenge of this ceremony matches the one the authenticator signed."""

    code = "passkey_challenge_expired"
    status = 400


class PasskeyAlreadyRegisteredError(ConflictError):
    code = "passkey_already_registered"


class UsernameTakenError(ConflictError):
    code = "username_taken"


class LastPasskeyError(ConflictError):
    """Deleting it would leave the account with no way to sign in."""

    code = "last_passkey"


class PasskeyUnknownError(UnauthorizedError):
    """Sign-in with a credential this server does not hold (never registered, or deleted)."""

    code = "passkey_unknown"


class AccountPasskeyNotFoundError(NotFoundError):
    code = "account_passkey_not_found"


class AccountSessionExpiredError(UnauthorizedError):
    code = "account_session_expired"


class OriginMismatchError(ForbiddenError):
    """A cookie-authenticated request that changes something came from another origin, or named none (CSRF)."""

    code = "origin_mismatch"


class ApiTokenInvalidError(UnauthorizedError):
    """A bearer token that is malformed, unknown or revoked; ``WWW-Authenticate`` says ``invalid_token`` (RFC 6750
    section 3.1, https://www.rfc-editor.org/rfc/rfc6750#section-3.1)."""

    code = "api_token_invalid"
    response_headers = (("WWW-Authenticate", 'Bearer error="invalid_token"'),)


class CookieChallengedUnauthorizedError(UnauthorizedError):
    """A session refusal (no cookie, or a dead session) on a route that takes only the session cookie, keeping its
    code. A 401 must carry a challenge (RFC 9110 section 15.5.2, https://www.rfc-editor.org/rfc/rfc9110#section-15.5.2);
    no registered scheme covers a cookie, so it names the ``Cookie`` scheme of the cookie authentication draft
    (https://datatracker.ietf.org/doc/html/draft-broyer-http-cookie-auth-00) with the session cookie's name."""

    response_headers = (("WWW-Authenticate", f'Cookie realm="pay-7331", cookie-name="{ACCOUNT_SESSION_COOKIE_NAME}"'),)

    def __init__(self, session_refusal: UnauthorizedError) -> None:
        super().__init__(session_refusal.message)
        self.code = session_refusal.code


class BearerChallengedUnauthorizedError(UnauthorizedError):
    """A cookie refusal (no cookie, or a dead session) on a route that also takes an API token, keeping its code. A 401
    must name a scheme the route accepts in ``WWW-Authenticate`` (RFC 9110 section 15.5.2,
    https://www.rfc-editor.org/rfc/rfc9110#section-15.5.2; RFC 6750 section 3,
    https://www.rfc-editor.org/rfc/rfc6750#section-3), and a request with no bearer token gets no error code there
    (RFC 6750 section 3.1)."""

    response_headers = (("WWW-Authenticate", "Bearer"),)

    def __init__(self, cookie_refusal: UnauthorizedError) -> None:
        super().__init__(cookie_refusal.message)
        self.code = cookie_refusal.code


class AccountApiTokenNotFoundError(NotFoundError):
    code = "api_token_not_found"


class TooManyApiTokensError(ConflictError):
    code = "too_many_api_tokens"


class PasskeyNotForThisAccountError(ForbiddenError):
    """A fresh passkey check signed by a passkey of another account (or by none of this account's)."""

    code = "passkey_not_for_this_account"


class PasskeyEnrollmentLinkUnknownError(NotFoundError):
    """No enrollment link has this token: mistyped, cut short, or replaced by a newer link for the account."""

    code = "passkey_enrollment_link_unknown"


class PasskeyEnrollmentLinkUsedError(AppError):
    code = "passkey_enrollment_link_used"
    status = 410


class PasskeyEnrollmentLinkExpiredError(AppError):
    code = "passkey_enrollment_link_expired"
    status = 410


class AccountNotFoundError(NotFoundError):
    code = "account_not_found"
