"""Where the sidecar answers (env ``PAY_MONERO_SIDECAR_URL``)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class MoneroSidecarSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PAY_", extra="ignore", hide_input_in_errors=True)

    # The sidecar's base URL on the compose network (``http://xmr-scanner:8750``). Empty, or no Monero chain enabled:
    # Monero wallet changes, XMR invoices and XMR links refuse with 503 `monero_sidecar_unavailable`, the watcher runs
    # no Monero loop (one log line at start), its healthcheck skips Monero chains and `reconcile` warns.
    monero_sidecar_url: str = ""


@lru_cache
def read_monero_sidecar_settings() -> MoneroSidecarSettings:
    return MoneroSidecarSettings()


def monero_sidecar_is_configured() -> bool:
    return bool(read_monero_sidecar_settings().monero_sidecar_url)
