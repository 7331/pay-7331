"""The ip-api.com pro key (env ``PAY_IP_API_KEY``)."""

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class IpApiSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PAY_", extra="ignore", hide_input_in_errors=True)

    # Empty means no lookup is possible; sign-up screening then admits or refuses by PAY_SCREENING_FAIL_CLOSED.
    ip_api_key: SecretStr = SecretStr("")


@lru_cache
def read_ip_api_settings() -> IpApiSettings:
    return IpApiSettings()
