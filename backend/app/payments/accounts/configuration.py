"""The WebAuthn relying party, from ``PAY_PUBLIC_BASE_URL``: its host is the RP id (``pay.7331.org``; ``localhost``
locally, since an IP address cannot be one, https://www.w3.org/TR/webauthn-3/#rp-id) and its origin is the only one
accepted in clientDataJSON and, for cookie requests that change something, in the ``Origin`` header."""

from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

RELYING_PARTY_NAME = "pay-7331"


class AccountPasskeySettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    public_base_url: str = Field(validation_alias="PAY_PUBLIC_BASE_URL")


class RelyingParty(BaseModel):
    model_config = ConfigDict(frozen=True)

    relying_party_id: str
    # scheme://host[:port], exactly as a browser writes it in clientDataJSON and the Origin header.
    expected_origin: str


def relying_party_from_public_base_url(public_base_url: str) -> RelyingParty:
    parts = urlsplit(public_base_url.strip())
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("PAY_PUBLIC_BASE_URL must be an http(s) origin")
    return RelyingParty(
        relying_party_id=parts.hostname,
        expected_origin=f"{parts.scheme}://{parts.netloc}".lower(),
    )


@lru_cache
def read_relying_party() -> RelyingParty:
    return relying_party_from_public_base_url(AccountPasskeySettings().public_base_url)
