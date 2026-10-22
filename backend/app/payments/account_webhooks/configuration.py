"""Webhook settings (env ``PAY_WEBHOOK_ALLOW_PRIVATE_HOSTS``; 2026-10-12) and the fork e2e's Discord stand-in
(``PAY_E2E_DISCORD_API_BASE_URL``; 2026-10-13)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.payments.chains.configuration import read_enabled_chains

# The anvil forks' chain keys (backend/chains.toml; Polygon's, and Arbitrum One's since 2026-10-14): a test-only switch is
# read only when every enabled chain is one of them (the two-chain fork stack enables both).
LOCAL_FORK_CHAIN_KEYS = frozenset({"local_fork", "local_arbitrum_fork"})


class AccountWebhookSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PAY_", extra="ignore")

    # Off everywhere but the e2e stack, whose receiver is a container on the stack's own network: lets a webhook URL be
    # plain http and resolve to a loopback, private or other non-global address. On in production it would let any
    # account make the API POST into the host's own networks (SSRF), so it is read only through
    # ``read_webhook_allow_private_hosts``, which ignores it unless only fork chains are enabled.
    webhook_allow_private_hosts: bool = False
    # Test-only (e2e/polygon_fork_flow.py): where Discord messages go instead of https://discord.com/api, e.g.
    # http://pay-7331-e2e-webhook-receiver:8080/api. Read through ``read_e2e_discord_api_base_url`` only.
    e2e_discord_api_base_url: str = ""


@lru_cache
def read_account_webhook_settings() -> AccountWebhookSettings:
    return AccountWebhookSettings()


def only_local_fork_chains_are_enabled() -> bool:
    """True when every enabled chain is an anvil fork (and at least one is): the only setup where the test-only
    switches are read. Production enables polygon, so they stay off there whatever the env file says."""
    enabled_chain_keys = {chain.key for chain in read_enabled_chains()}
    return bool(enabled_chain_keys) and enabled_chain_keys <= LOCAL_FORK_CHAIN_KEYS


@lru_cache
def read_webhook_allow_private_hosts() -> bool:
    """``PAY_WEBHOOK_ALLOW_PRIVATE_HOSTS`` when only fork chains are enabled, else False."""
    return only_local_fork_chains_are_enabled() and read_account_webhook_settings().webhook_allow_private_hosts


@lru_cache
def read_e2e_discord_api_base_url() -> str:
    """``PAY_E2E_DISCORD_API_BASE_URL`` when only fork chains are enabled, else "": the stand-in can never take a
    real account's Discord messages in production."""
    if not only_local_fork_chains_are_enabled():
        return ""
    return read_account_webhook_settings().e2e_discord_api_base_url.rstrip("/")
