"""Wire bodies of the account, passkey and session routes (PLAN.md section 7.1)."""

from datetime import datetime
from typing import Annotated

from pydantic import Field, JsonValue

from app.shared.schemas import ApiRequest, ApiResponse, SingleLineText

ADDRESS_PATTERN = r"^0x[0-9a-fA-F]{40}$"


class SignUpOptionsRequest(ApiRequest):
    # 3 to 20 of a-z, 0-9 and _, any case (stored lowercased).
    username: str = Field(min_length=3, max_length=20, pattern=r"^[A-Za-z0-9_]+$")
    payout_address: str = Field(pattern=ADDRESS_PATTERN)


class PasskeyCredentialRequest(ApiRequest):
    """Sign-up and adding a passkey: the new passkey's name and `credential.toJSON()` of what
    navigator.credentials.create() returned."""

    label: SingleLineText = Field(min_length=1, max_length=100)
    credential: dict[str, JsonValue]


class PasskeyEnrollmentOptionsRequest(ApiRequest):
    """The token from an enrollment link's fragment (`/enroll-passkey#<token>`)."""

    token: str = Field(min_length=1, max_length=128)


class EnrollPasskeyRequest(PasskeyCredentialRequest):
    """The enrollment link's token with the new passkey's name and credential, as when adding a passkey."""

    token: str = Field(min_length=1, max_length=128)


class SignInWithPasskeyRequest(ApiRequest):
    # `credential.toJSON()` of the PublicKeyCredential that navigator.credentials.get() returned.
    credential: dict[str, JsonValue]


class AccountPasskeyResponse(ApiResponse):
    # `pk_...`
    id: str
    label: str
    created_at: datetime
    last_used_at: datetime | None
    # True for the passkey this session signed in with (`/settings` marks it "Signed in with this").
    used_by_current_session: bool


class FreshPasskeyCheckRequest(ApiRequest):
    """`credential.toJSON()` of what navigator.credentials.get() returned for a fresh passkey check's options:
    `POST /account-passkeys/registration-options` (from `POST /account-passkeys/addition-options`) and
    `POST /account-passkeys/{id}/deletion` (from `POST /account-passkeys/{id}/deletion-options`)."""

    credential: dict[str, JsonValue]


class RenameAccountPasskeyRequest(ApiRequest):
    """PATCH /account-passkeys/{id}: the passkey's new name."""

    label: SingleLineText = Field(min_length=1, max_length=100)


class AccountPasskeyListResponse(ApiResponse):
    passkeys: list[AccountPasskeyResponse]


class AccountSessionResponse(ApiResponse):
    username: str
    payout_address: str
    # Every enabled chain's key and the coins its invoices start with ticked: saved, else the chain's defaults.
    accepted_assets_by_chain: dict[str, list[str]]
    # Every enabled Monero chain's key and the confirmations its XMR invoices count final at: the account's saved
    # number where it set one; a chain left out stands at the chain's default.
    confirmations_final_by_chain: dict[str, int]
    # As saved (an email address, `@telegram_name` or an https link); null when unset.
    support_contact: str | None


# One chain's coins, as `assets` in POST /invoices: 1 to 16 symbols, matched case-insensitively.
AcceptedAssetSymbols = Annotated[
    list[Annotated[str, Field(min_length=1, max_length=20)]], Field(min_length=1, max_length=16)
]

# One Monero chain's confirmations before an output counts final: 1 is trusting, the chain default is chains.toml's.
ConfirmationsFinal = Annotated[int, Field(ge=1, le=100)]


class UpdateCurrentAccountRequest(ApiRequest):
    """PATCH /accounts/current: only the fields sent change. A chain left out of `accepted_assets_by_chain` keeps its
    saved coins."""

    accepted_assets_by_chain: dict[str, AcceptedAssetSymbols] | None = Field(default=None, max_length=8)
    # The confirmations an XMR invoice counts final at, per enabled Monero chain key. A chain left
    # out keeps its saved number; a number below the chain default is the receiver accepting the reorg risk; `null`
    # clears the chain's saved number back to the default.
    confirmations_final_by_chain: dict[str, ConfirmationsFinal | None] | None = Field(default=None, max_length=8)
    # An email address, a Telegram handle (`@name`, 5 to 32 of A-Z, a-z, 0-9, _) or an https link; "" or null clears
    # it. Left out, it stays.
    support_contact: SingleLineText | None = Field(default=None, max_length=200)


class PayoutAddressChangeOptionsRequest(ApiRequest):
    """POST /accounts/current/payout-address-change-options: the address the passkey check will approve."""

    payout_address: str = Field(pattern=ADDRESS_PATTERN)


class CreateApiTokenRequest(ApiRequest):
    """POST /account-api-tokens: the token's name and `credential.toJSON()` of the PublicKeyCredential that
    navigator.credentials.get() returned for `POST /account-api-tokens/creation-options`."""

    name: SingleLineText = Field(min_length=1, max_length=100)
    credential: dict[str, JsonValue]


class AccountApiTokenResponse(ApiResponse):
    # `tok_...`
    id: str
    name: str
    created_at: datetime
    last_used_at: datetime | None


class CreatedAccountApiTokenResponse(AccountApiTokenResponse):
    # `pay_tok_...`: in this answer only; it is never shown again.
    token: str


class AccountApiTokenListResponse(ApiResponse):
    api_tokens: list[AccountApiTokenResponse]


class ChangePayoutAddressRequest(ApiRequest):
    """PUT /accounts/current/payout-address: the same address and `credential.toJSON()` of the PublicKeyCredential
    that navigator.credentials.get() returned for those options."""

    payout_address: str = Field(pattern=ADDRESS_PATTERN)
    credential: dict[str, JsonValue]
