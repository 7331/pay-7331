"""The account's Monero wallet routes, session only. Here, not in accounts or monero_wallets: the view reads this
domain's Monero payments, and the change needs the accounts domain's passkey check and records an account event
(monero_wallet_settings_service.py)."""

from fastapi import APIRouter, Query, Response
from pydantic import JsonValue

from app.payments.accounts.dependencies import AccountAccessDependency
from app.payments.accounts.errors import (
    OriginMismatchError,
    PasskeyChallengeExpiredError,
    PasskeyNotForThisAccountError,
    PasskeyResponseInvalidError,
)
from app.payments.chains.errors import ChainKindNotSupportedError, ChainNotEnabledError
from app.payments.invoices.dependencies import MoneroWalletSettingsServiceDependency
from app.payments.invoices.schemas import (
    ConfirmMoneroLookaheadRequest,
    MoneroWalletChangeOptionsRequest,
    MoneroWalletSettingsResponse,
    SetMoneroWalletRequest,
)
from app.payments.monero_wallets.errors import (
    MoneroAddressInvalidError,
    MoneroAddressTakenError,
    MoneroSidecarUnavailableError,
    MoneroViewKeyMismatchError,
    MoneroWalletChangeConflictError,
    MoneroWalletMissingError,
    MoneroWalletUnchangedError,
)
from app.shared.errors import InvalidInputError
from app.shared.openapi_error_responses import describe_error_responses
from app.shared.path_params import API_PREFIX

monero_wallet_router = APIRouter(prefix=API_PREFIX, tags=["monero wallet"])

NO_STORE_HEADERS = {"Cache-Control": "no-store"}
MONERO_CHAIN_ERRORS = (InvalidInputError, ChainNotEnabledError, ChainKindNotSupportedError)


@monero_wallet_router.get("/account-monero-wallet", responses=describe_error_responses(*MONERO_CHAIN_ERRORS))
async def read_account_monero_wallet(
    *,
    response: Response,
    account_access: AccountAccessDependency,
    monero_wallet_settings_service: MoneroWalletSettingsServiceDependency,
    chain_key: str | None = Query(default=None, max_length=32),
) -> MoneroWalletSettingsResponse:
    """The enabled Monero chains and the account's wallet of ``chain_key`` (the first Monero chain when left out), or
    null; never the view key."""
    response.headers.update(NO_STORE_HEADERS)
    return await monero_wallet_settings_service.read_wallet_settings(account_access.account_id, chain_key=chain_key)


@monero_wallet_router.post(
    "/account-monero-wallet/change-options",
    responses=describe_error_responses(*MONERO_CHAIN_ERRORS, OriginMismatchError),
)
async def create_account_monero_wallet_change_options(
    options_request: MoneroWalletChangeOptionsRequest,
    *,
    response: Response,
    account_access: AccountAccessDependency,
    monero_wallet_settings_service: MoneroWalletSettingsServiceDependency,
) -> dict[str, JsonValue]:
    """WebAuthn request options for the passkey check a wallet change needs, bound to the wallet in the body."""
    response.headers.update(NO_STORE_HEADERS)
    return await monero_wallet_settings_service.create_change_options(
        account_access, chain_key=options_request.chain_key, primary_address=options_request.primary_address
    )


@monero_wallet_router.put(
    "/account-monero-wallet",
    responses=describe_error_responses(
        *MONERO_CHAIN_ERRORS,
        MoneroAddressInvalidError,
        MoneroViewKeyMismatchError,
        PasskeyResponseInvalidError,
        PasskeyChallengeExpiredError,
        OriginMismatchError,
        PasskeyNotForThisAccountError,
        MoneroWalletUnchangedError,
        MoneroAddressTakenError,
        MoneroWalletChangeConflictError,
        MoneroSidecarUnavailableError,
    ),
)
async def set_account_monero_wallet(
    set_request: SetMoneroWalletRequest,
    *,
    response: Response,
    account_access: AccountAccessDependency,
    monero_wallet_settings_service: MoneroWalletSettingsServiceDependency,
) -> MoneroWalletSettingsResponse:
    """Sets the account's wallet of ``chain_key`` once the scanner accepts the address and view key and the passkey
    check made for this wallet verifies. Invoices already made keep their wallet."""
    response.headers.update(NO_STORE_HEADERS)
    return await monero_wallet_settings_service.set_wallet(
        account_access,
        chain_key=set_request.chain_key,
        primary_address=set_request.primary_address,
        private_view_key=set_request.private_view_key,
        credential_json=set_request.credential,
    )


@monero_wallet_router.post(
    "/account-monero-wallet/lookahead-confirmations",
    responses=describe_error_responses(*MONERO_CHAIN_ERRORS, OriginMismatchError, MoneroWalletMissingError),
)
async def confirm_account_monero_wallet_lookahead(
    confirmation_request: ConfirmMoneroLookaheadRequest,
    *,
    response: Response,
    account_access: AccountAccessDependency,
    monero_wallet_settings_service: MoneroWalletSettingsServiceDependency,
) -> MoneroWalletSettingsResponse:
    """The receiver ran ``set subaddress-lookahead 50:1000``: the wallet's gap guard trusts 1000 minors."""
    response.headers.update(NO_STORE_HEADERS)
    return await monero_wallet_settings_service.confirm_lookahead(
        account_access, chain_key=confirmation_request.chain_key
    )
