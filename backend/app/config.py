"""Settings no package owns: the two DSNs, the log level and the public origin."""

from functools import lru_cache
import ipaddress
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ApplicationSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    database_url: str = Field(validation_alias="DATABASE_URL", repr=False)
    redis_url: str = Field(validation_alias="REDIS_URL", repr=False)
    # The origin pages and links are served from (`https://pay.7331.org`); invoice URLs and refund texts name it.
    public_base_url: str = Field(validation_alias="PAY_PUBLIC_BASE_URL")
    # Comma-separated peer IPs whose X-Real-IP is the client IP (the host Caddy, app/shared/request.py); empty: none.
    trusted_proxy_addresses_text: str = Field(default="", validation_alias="PAY_TRUSTED_PROXY_ADDRESSES")
    # true on the testnet sandbox (pay-sandbox.7331.org, PLAN.md section 16): every page shows "Sandbox — testnet
    # money only" (app/payments/pages/router.py marks the page for the app).
    sandbox: bool = Field(default=False, validation_alias="PAY_SANDBOX")

    @property
    def trusted_proxy_addresses(self) -> frozenset[str]:
        return frozenset(
            str(ipaddress.ip_address(address.strip()))
            for address in self.trusted_proxy_addresses_text.split(",")
            if address.strip()
        )


class LoggingSettings(BaseSettings):
    """Read when a process configures logging, before any other setting, so it has a default."""

    model_config = SettingsConfigDict(extra="ignore")

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(default="INFO", validation_alias="LOG_LEVEL")


@lru_cache
def read_application_settings() -> ApplicationSettings:
    return ApplicationSettings()


@lru_cache
def read_logging_settings() -> LoggingSettings:
    return LoggingSettings()
