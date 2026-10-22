"""Account, passkey and session routes, thin (PLAN.md sections 7 and 7.1).

Sign-up (`/accounts/...`) and sign-in (`/sessions`) are public and rate-limited as public writes; both set the session
cookie. Listing and renaming passkeys need the cookie; adding and deleting one also a fresh passkey check, and they
record an account event, so those routes live in account_events. The API token routes need the cookie, and making a
token a fresh passkey check (2026-10-12). The options routes return WebAuthn's JSON forms, which the pages hand to
``PublicKeyCredential.parseCreationOptionsFromJSON`` / ``parseRequestOptionsFromJSON``.
"""

from typing import Annotated

from fastapi import APIRouter, Path, Response
from pydantic import JsonValue

from app.lifespan.dependencies import ClientIpAddressDependency, ResourcesDependency
from app.payments.accounts.dependencies import (
    ACCOUNT_SESSION_COOKIE_NAME,
    AccountAccessDependency,
    AccountApiTokenServiceDependency,
    AccountPasskeyServiceDependency,
    AccountPreferencesServiceDependency,
    AccountServiceDependency,
    AccountSessionTokenDependency,
    OptionalAccountAccessDependency,
)
from app.payments.accounts.schemas import (
    AccountApiTokenListResponse,
    AccountApiTokenResponse,
    AccountPasskeyListResponse,
    AccountPasskeyResponse,
    AccountSessionResponse,
    CreateApiTokenRequest,
    CreatedAccountApiTokenResponse,
    PasskeyCredentialRequest,
    RenameAccountPasskeyRequest,
    SignInWithPasskeyRequest,
    SignUpOptionsRequest,
    UpdateCurrentAccountRequest,
)
from app.payments.accounts.service import ACCOUNT_SESSION_LIFETIME
from app.payments.accounts.types import (
    AccountAccess,
    AccountApiTokenSummary,
    AccountPasskeySummary,
    StartedAccountSession,
)
from app.payments.sign_up_screening.dependencies import SignUpScreeningServiceDependency
from app.shared.path_params import API_PREFIX, AccountPasskeyIdPath

accounts_router = APIRouter(prefix=API_PREFIX)

NO_STORE_HEADERS = {"Cache-Control": "no-store"}
# Public ids: a prefix and 22 base58 characters (app/shared/identifiers.py).
AccountApiTokenIdPath = Annotated[str, Path(pattern=r"^tok_[1-9A-HJ-NP-Za-km-z]{22}$")]


def present_account_passkey_response(summary: AccountPasskeySummary) -> AccountPasskeyResponse:
    return AccountPasskeyResponse(
        id=summary.account_passkey_public_id,
        label=summary.label,
        created_at=summary.created_at,
        last_used_at=summary.last_used_at,
        used_by_current_session=summary.used_by_current_session,
    )


def present_account_session_response(account_access: AccountAccess) -> AccountSessionResponse:
    return AccountSessionResponse(
        username=account_access.username,
        payout_address=account_access.payout_address,
        accepted_assets_by_chain=account_access.accepted_asset_symbols_by_chain,
        confirmations_final_by_chain=account_access.confirmations_final_by_chain,
        invoice_lifetime_seconds_by_chain=account_access.invoice_lifetime_seconds_by_chain,
        accuracy_percent_by_chain=account_access.accuracy_percent_by_chain,
        support_contact=account_access.support_contact,
    )


def start_session_response(response: Response, *, started_session: StartedAccountSession) -> AccountSessionResponse:
    response.set_cookie(
        ACCOUNT_SESSION_COOKIE_NAME,
        started_session.session_token.get_secret_value(),
        max_age=int(ACCOUNT_SESSION_LIFETIME.total_seconds()),
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )
    response.headers.update(NO_STORE_HEADERS)
    return AccountSessionResponse(
        username=started_session.username,
        payout_address=started_session.payout_address,
        accepted_assets_by_chain=started_session.accepted_asset_symbols_by_chain,
        confirmations_final_by_chain=started_session.confirmations_final_by_chain,
        invoice_lifetime_seconds_by_chain=started_session.invoice_lifetime_seconds_by_chain,
        accuracy_percent_by_chain=started_session.accuracy_percent_by_chain,
        support_contact=started_session.support_contact,
    )


@accounts_router.post("/accounts/registration-options", tags=["accounts"])
async def create_sign_up_options(
    options_request: SignUpOptionsRequest,
    *,
    response: Response,
    account_service: AccountServiceDependency,
    client_ip_address: ClientIpAddressDependency,
    sign_up_screening_service: SignUpScreeningServiceDependency,
    resources: ResourcesDependency,
) -> dict[str, JsonValue]:
    """WebAuthn registration options (``PublicKeyCredentialCreationOptionsJSON``) for a new account with this username
    and payout address. Sign-up screening runs first, so a refused visitor is told before making a passkey."""
    await sign_up_screening_service.screen_client_ip_address(client_ip_address)
    response.headers.update(NO_STORE_HEADERS)
    return await account_service.create_sign_up_options(
        username=options_request.username,
        payout_address=options_request.payout_address,
        gateway_deposit_address_check=resources.invoice_wiring.gateway_deposit_address_check,
    )


@accounts_router.post("/accounts", status_code=201, tags=["accounts"])
async def sign_up(
    sign_up_request: PasskeyCredentialRequest,
    *,
    response: Response,
    account_service: AccountServiceDependency,
    client_ip_address: ClientIpAddressDependency,
    sign_up_screening_service: SignUpScreeningServiceDependency,
) -> AccountSessionResponse:
    """Makes the account and its first passkey from the credential made for those options, and signs in: sets the
    session cookie and answers the session."""
    # Screened again (the verdict is cached an hour): nothing makes a client call the options route first from the
    # same network.
    await sign_up_screening_service.screen_client_ip_address(client_ip_address)
    started_session = await account_service.sign_up(sign_up_request.credential, label=sign_up_request.label)
    return start_session_response(response, started_session=started_session)


@accounts_router.get("/account-passkeys", tags=["passkeys"])
async def list_account_passkeys(
    *, account_access: AccountAccessDependency, account_passkey_service: AccountPasskeyServiceDependency
) -> AccountPasskeyListResponse:
    return AccountPasskeyListResponse(
        passkeys=[
            present_account_passkey_response(summary)
            for summary in await account_passkey_service.list_passkeys(account_access)
        ]
    )


@accounts_router.patch("/account-passkeys/{account_passkey_id}", tags=["passkeys"])
async def rename_account_passkey(
    account_passkey_id: AccountPasskeyIdPath,
    *,
    rename_request: RenameAccountPasskeyRequest,
    account_access: AccountAccessDependency,
    account_passkey_service: AccountPasskeyServiceDependency,
) -> AccountPasskeyResponse:
    summary = await account_passkey_service.rename_passkey(
        account_access, account_passkey_public_id=account_passkey_id, label=rename_request.label
    )
    return present_account_passkey_response(summary)


@accounts_router.post("/sessions/authentication-options", tags=["accounts"])
async def create_sign_in_options(
    *, response: Response, account_service: AccountServiceDependency
) -> dict[str, JsonValue]:
    response.headers.update(NO_STORE_HEADERS)
    return await account_service.create_sign_in_options()


@accounts_router.post("/sessions", status_code=201, tags=["accounts"])
async def sign_in_with_passkey(
    sign_in_request: SignInWithPasskeyRequest, *, response: Response, account_service: AccountServiceDependency
) -> AccountSessionResponse:
    started_session = await account_service.sign_in_with_passkey(sign_in_request.credential)
    return start_session_response(response, started_session=started_session)


@accounts_router.get("/sessions/current", tags=["accounts"])
async def read_current_session(
    *, response: Response, account_access: OptionalAccountAccessDependency
) -> AccountSessionResponse | None:
    """The signed-in session, or ``null`` when signed out (never 401)."""
    # Null rather than 401: the pages ask on every load, and a 401 there would be a console error per visit.
    response.headers.update(NO_STORE_HEADERS)
    if account_access is None:
        return None
    return present_account_session_response(account_access)


@accounts_router.patch("/accounts/current", tags=["accounts"])
async def update_current_account(
    update_request: UpdateCurrentAccountRequest,
    *,
    response: Response,
    account_access: AccountAccessDependency,
    account_preferences_service: AccountPreferencesServiceDependency,
) -> AccountSessionResponse:
    """The signed-in account's settings: the coins its invoices start with ticked, per chain, the confirmations its
    XMR invoices count final at, per Monero chain, the payment window and accepted shortfall its invoices take per
    chain when their maker sets none, and the support contact its payers see. Only
    the fields sent change. Answers the session view as ``GET /sessions/current`` would after the change. The payout
    address is not here: it changes only behind a fresh passkey check
    (``PUT /accounts/current/payout-address``)."""
    response.headers.update(NO_STORE_HEADERS)
    if update_request.accepted_assets_by_chain is not None:
        accepted_asset_symbols_by_chain = await account_preferences_service.save_accepted_asset_symbols(
            account_access, accepted_asset_symbols_by_chain=update_request.accepted_assets_by_chain
        )
        account_access = account_access.with_accepted_asset_symbols_by_chain(accepted_asset_symbols_by_chain)
    if "support_contact" in update_request.model_fields_set:
        support_contact = await account_preferences_service.save_support_contact(
            account_access, support_contact=update_request.support_contact
        )
        account_access = account_access.with_support_contact(support_contact)
    if update_request.confirmations_final_by_chain is not None:
        confirmations_final_by_chain = await account_preferences_service.save_confirmations_final_by_chain(
            account_access, confirmations_final_by_chain=update_request.confirmations_final_by_chain
        )
        account_access = account_access.with_confirmations_final_by_chain(confirmations_final_by_chain)
    if (
        update_request.invoice_lifetime_seconds_by_chain is not None
        or update_request.accuracy_percent_by_chain is not None
    ):
        (
            invoice_lifetime_seconds_by_chain,
            accuracy_percent_by_chain,
        ) = await account_preferences_service.save_invoice_payment_terms_by_chain(
            account_access,
            invoice_lifetime_seconds_by_chain=update_request.invoice_lifetime_seconds_by_chain or {},
            accuracy_percent_by_chain=update_request.accuracy_percent_by_chain or {},
        )
        account_access = account_access.with_invoice_payment_terms_by_chain(
            invoice_lifetime_seconds_by_chain=invoice_lifetime_seconds_by_chain,
            accuracy_percent_by_chain=accuracy_percent_by_chain,
        )
    return present_account_session_response(account_access)


def present_account_api_token_response(summary: AccountApiTokenSummary) -> AccountApiTokenResponse:
    return AccountApiTokenResponse(
        id=summary.account_api_token_public_id,
        name=summary.name,
        created_at=summary.created_at,
        last_used_at=summary.last_used_at,
    )


@accounts_router.post("/account-api-tokens/creation-options", tags=["accounts"])
async def create_api_token_options(
    *,
    response: Response,
    account_access: AccountAccessDependency,
    account_api_token_service: AccountApiTokenServiceDependency,
) -> dict[str, JsonValue]:
    """WebAuthn request options for the fresh passkey check a new API token needs."""
    response.headers.update(NO_STORE_HEADERS)
    return await account_api_token_service.create_api_token_options(account_access)


@accounts_router.post("/account-api-tokens", status_code=201, tags=["accounts"])
async def create_api_token(
    token_request: CreateApiTokenRequest,
    *,
    response: Response,
    account_access: AccountAccessDependency,
    account_api_token_service: AccountApiTokenServiceDependency,
) -> CreatedAccountApiTokenResponse:
    """A new API token once the passkey check verifies; ``token`` is in this answer only."""
    response.headers.update(NO_STORE_HEADERS)
    created = await account_api_token_service.create_api_token(
        account_access, name=token_request.name, credential_json=token_request.credential
    )
    return CreatedAccountApiTokenResponse(
        id=created.summary.account_api_token_public_id,
        name=created.summary.name,
        created_at=created.summary.created_at,
        last_used_at=created.summary.last_used_at,
        token=created.token.get_secret_value(),
    )


@accounts_router.get("/account-api-tokens", tags=["accounts"])
async def list_api_tokens(
    *,
    response: Response,
    account_access: AccountAccessDependency,
    account_api_token_service: AccountApiTokenServiceDependency,
) -> AccountApiTokenListResponse:
    response.headers.update(NO_STORE_HEADERS)
    return AccountApiTokenListResponse(
        api_tokens=[
            present_account_api_token_response(summary)
            for summary in await account_api_token_service.list_api_tokens(account_access)
        ]
    )


@accounts_router.delete("/account-api-tokens/{account_api_token_id}", status_code=204, tags=["accounts"])
async def revoke_api_token(
    account_api_token_id: AccountApiTokenIdPath,
    *,
    account_access: AccountAccessDependency,
    account_api_token_service: AccountApiTokenServiceDependency,
) -> None:
    """Revokes at once: the next bearer request with it is 401."""
    await account_api_token_service.revoke_api_token(account_access, account_api_token_public_id=account_api_token_id)


@accounts_router.delete("/sessions/current", status_code=204, tags=["accounts"])
async def sign_out(
    *,
    response: Response,
    _account_access: AccountAccessDependency,
    session_token: AccountSessionTokenDependency,
    account_service: AccountServiceDependency,
) -> None:
    """Ends the cookie's session and clears the cookie."""
    if session_token is not None:
        await account_service.sign_out(session_token)
    response.delete_cookie(ACCOUNT_SESSION_COOKIE_NAME, path="/", secure=True, httponly=True, samesite="strict")
