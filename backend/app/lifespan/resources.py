"""The clients one process holds, as frozen, typed models.

``app.lifespan.composition`` opens them on one AsyncExitStack, so they close in reverse order even when startup fails
halfway.
"""

from typing import TypedDict

from pydantic import BaseModel, ConfigDict

from app.infrastructure.postgres.client import Database
from app.infrastructure.rate_limit.limiter import RateLimiter
from app.infrastructure.redis.client import RedisClient
from app.payments.account_events.stream_service import AccountEventStreamService
from app.payments.account_webhooks.delivery_service import AccountWebhookDeliveryService
from app.payments.chain_watcher.ip_api_key_alert_service import IpApiKeyAlertService
from app.payments.chain_watcher.monero_service import MoneroChainWatcherService
from app.payments.chain_watcher.service import ChainWatcherService
from app.payments.chain_watcher.watcher_liveness_service import WatcherLivenessService
from app.payments.chains.service import ChainsService
from app.payments.invoice_schedules.runner_service import ScheduledInvoiceRunner
from app.payments.invoices.wiring import InvoiceWiring


class StoreResources(BaseModel):
    """Postgres and Redis, which every process role holds."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    database: Database
    redis_client: RedisClient


class ApiResources(StoreResources):
    rate_limiter: RateLimiter
    trusted_proxy_addresses: frozenset[str]
    chains_service: ChainsService
    invoice_wiring: InvoiceWiring
    # The API's own loops (``app.lifespan.lifespan``).
    watcher_liveness_service: WatcherLivenessService
    # Alerts the operator once ip-api rejects the screening key (2026-10-13).
    ip_api_key_alert_service: IpApiKeyAlertService
    # Sends queued messages to accounts' webhooks every 2 s (2026-10-12).
    account_webhook_delivery_service: AccountWebhookDeliveryService
    # The account event streams' shared poller and their subscriptions, this process's open streams only.
    account_event_stream_service: AccountEventStreamService


class ApiLifespanState(TypedDict):
    """What the API's lifespan yields: Starlette hands each request a shallow copy of it in ``scope["state"]``
    ("Lifespan State", https://github.com/encode/starlette/blob/master/docs/lifespan.md)."""

    resources: ApiResources


class WatcherResources(StoreResources):
    invoice_wiring: InvoiceWiring
    chain_watchers: list[ChainWatcherService]
    monero_chain_watchers: list[MoneroChainWatcherService]
    # Makes each invoice schedule's due invoice (PLAN.md section 18, 2026-10-21).
    scheduled_invoice_runner: ScheduledInvoiceRunner


class CliInvoiceResources(StoreResources):
    """``sweep-invoice``: the stores and the invoice wiring, no watcher loops."""

    invoice_wiring: InvoiceWiring
