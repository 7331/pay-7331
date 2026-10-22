"""Static deposit address settings (env ``PAY_MAXIMUM_STATIC_DEPOSIT_ADDRESSES_PER_ACCOUNT``; PLAN.md section 17)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class StaticDepositAddressSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PAY_", extra="ignore")

    # Every static address is scanned for good, so this bounds what one account adds to the
    # watcher's scan (its per-tick reads are capped either way; more addresses lengthen a pass).
    maximum_static_deposit_addresses_per_account: int = 1_000


@lru_cache
def read_static_deposit_address_settings() -> StaticDepositAddressSettings:
    return StaticDepositAddressSettings()
