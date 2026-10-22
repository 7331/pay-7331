"""Sign-up screening flags and the IP hash salt (env ``PAY_SCREENING_*``, ``PAY_IP_HASH_SALT``; PLAN.md section 9)."""

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ScreeningSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PAY_", extra="ignore", hide_input_in_errors=True)

    # VPN, proxy and Tor: off by default, many crypto payers use VPNs.
    screening_block_proxy: bool = False
    # Data centres: on by default.
    screening_block_hosting: bool = True
    # Comma-separated ISO 3166-1 alpha-2 codes.
    screening_blocked_country_codes: str = ""
    # Refuse a sign-up when ip-api cannot answer (off: admit and log).
    screening_fail_closed: bool = False
    # Salts the sha256 of a client IP (rate limits, the open-invoice cap, the screening cache); the IP itself is never
    # stored or logged.
    ip_hash_salt: SecretStr


@lru_cache
def read_screening_settings() -> ScreeningSettings:
    return ScreeningSettings()
