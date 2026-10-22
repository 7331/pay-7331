"""Open each process role's clients once, wire the long-lived services, and run the process loops.

Startup refuses: a schema behind Alembic head, an RPC endpoint serving another chain id, (watcher) a chain with a
factory address but no operator key, and (API) any operator key variable that is set. The internet-facing API never
holds an operator key, not even its address (2026-10-13): a sweep it asks for names the zero address and the runner
picks a sweeper when it signs, a refund is always signed by the refund key, and the compose files blank the key
variables for the api service. The watcher and the CLI (run in the watcher container) read the keys. A chain with no factory address yet is logged once and not watched (no invoice
can exist on it: invoice creation refuses with ``factory_not_deployed``). Migrations are a deploy step (the compose
``migrate`` service).
"""

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import read_application_settings
from app.infrastructure.chainlink_api.client import ChainlinkApiClient
from app.infrastructure.deposit_factory_api.client import DepositFactoryApiClient
from app.infrastructure.evm_rpc_api.client import EvmRpcClient
from app.infrastructure.ip_api.client import IpApiClient
from app.infrastructure.ip_api.configuration import read_ip_api_settings
from app.infrastructure.monero_sidecar_api.client import MoneroSidecarApiClient
from app.infrastructure.monero_sidecar_api.configuration import (
    monero_sidecar_is_configured,
    read_monero_sidecar_settings,
)
from app.infrastructure.postgres.client import Database
from app.infrastructure.rate_limit.limiter import RateLimiter
from app.infrastructure.redis.client import RedisClient
from app.lifespan.resources import ApiResources, CliInvoiceResources, WatcherResources
from app.log_events import (
    ChainNotReadyForWatching,
    MoneroChainsIdle,
    OperatorAccountNotConfigured,
    OperatorAccountNotFound,
    ProcessLoopPassFailed,
    ProcessLoopStopped,
)
from app.payments.account_events.stream_service import (
    POLL_INTERVAL_SECONDS as ACCOUNT_EVENT_POLL_INTERVAL_SECONDS,
)
from app.payments.account_events.stream_service import AccountEventStreamService
from app.payments.account_webhooks.configuration import read_webhook_allow_private_hosts
from app.payments.account_webhooks.delivery_service import (
    DELIVERY_INTERVAL_SECONDS as WEBHOOK_DELIVERY_INTERVAL_SECONDS,
)
from app.payments.account_webhooks.delivery_service import AccountWebhookDeliveryService
from app.payments.account_webhooks.secret_encryption import derive_webhook_secret_encryption_key
from app.payments.account_webhooks.service import check_invoice_callback_url
from app.payments.accounts.service import AccountService
from app.payments.chain_watcher.ip_api_key_alert_service import (
    IP_API_KEY_CHECK_INTERVAL_SECONDS,
    IpApiKeyAlertService,
)
from app.payments.chain_watcher.monero_service import MoneroChainWatcherService
from app.payments.chain_watcher.operator_alerts_service import OperatorAlertService
from app.payments.chain_watcher.rescan_service import DepositRescanService
from app.payments.chain_watcher.service import ChainWatcherService
from app.payments.chain_watcher.static_deposit_scan_service import StaticDepositScanService
from app.payments.chain_watcher.watcher_liveness_service import (
    WATCHER_LIVENESS_CHECK_INTERVAL_SECONDS,
    WatcherLivenessService,
)
from app.payments.chains.configuration import read_enabled_chains
from app.payments.chains.service import ChainsService
from app.payments.chains.types import EvmChainConfiguration
from app.payments.invoice_schedules.runner_service import ScheduledInvoiceRunner
from app.payments.invoices.chain_readings_service import ChainReadingsService
from app.payments.invoices.configuration import read_invoice_settings
from app.payments.invoices.wiring import InvoiceWiring
from app.payments.monero_wallets.secret_sealing import derive_private_view_key_encryption_key
from app.payments.monero_wallets.types import MoneroSidecar, UnavailableMoneroSidecar
from app.payments.settlements.configuration import find_set_operator_key_variable_names
from app.payments.settlements.operator_key_maintenance_service import OperatorKeyMaintenanceService
from app.payments.settlements.operator_keys import OperatorKeyAddresses, OperatorKeyRing, read_operator_key_ring
from app.payments.settlements.runner_service import SettlementRunner
from app.payments.settlements.types import SettlementOutcome, StaticDepositSweepOutcome
from app.payments.sign_up_screening.configuration import read_screening_settings
from app.payments.static_deposit_addresses.readings_service import StaticDepositReadingsService
from app.payments.static_deposit_addresses.service import is_gateway_deposit_address
from app.shared import logging as log

logger = log.get_logger(__name__)

API_DATABASE_POOL_SIZE = 5
# The API's side transaction engine: a fresh passkey check's consumption commits there (Database's docstring).
API_SIDE_TRANSACTION_POOL_SIZE = 2
WATCHER_DATABASE_POOL_SIZE = 3
CLI_DATABASE_POOL_SIZE = 1


async def open_store_resources(
    exit_stack: AsyncExitStack, *, application_name: str, pool_size: int, side_transaction_pool_size: int | None = None
) -> tuple[Database, RedisClient]:
    settings = read_application_settings()
    database = Database(
        settings.database_url,
        application_name=application_name,
        pool_size=pool_size,
        maximum_overflow=pool_size,
        side_transaction_pool_size=side_transaction_pool_size,
    )
    exit_stack.push_async_callback(database.close)
    await database.assert_at_head()
    redis_client = RedisClient(settings.redis_url, client_name=application_name)
    exit_stack.push_async_callback(redis_client.close)
    await redis_client.initialize()
    return database, redis_client


def read_operator_key_rings() -> dict[str, OperatorKeyRing]:
    """Each enabled chain's refund and sweeper keys; a chain with no refund key is absent."""
    return {
        chain.key: key_ring
        for chain in read_enabled_chains()
        if isinstance(chain, EvmChainConfiguration) and (key_ring := read_operator_key_ring(chain.key)) is not None
    }


async def open_monero_sidecar(exit_stack: AsyncExitStack, *, chains_service: ChainsService) -> MoneroSidecar:
    """The scanner sidecar's client for every Monero port (PLAN.md section 14): wallet checks, subaddresses, the tip and
    txid checks. Only with a Monero chain enabled and ``PAY_MONERO_SIDECAR_URL`` set; otherwise every call is
    ``monero_sidecar_unavailable`` (503), so wallets cannot be set, XMR invoices are refused and Monero ticks credit
    nothing. No redirects and no proxy from the environment: the URL is the compose network's own."""
    monero_sidecar_url = read_monero_sidecar_settings().monero_sidecar_url
    if not chains_service.monero_chains or not monero_sidecar_url:
        return UnavailableMoneroSidecar()
    monero_sidecar_http_client = await exit_stack.enter_async_context(
        httpx.AsyncClient(follow_redirects=False, trust_env=False)
    )
    return MoneroSidecarApiClient(http_client=monero_sidecar_http_client, base_url=monero_sidecar_url)


def addresses_of(operator_key_rings: dict[str, OperatorKeyRing]) -> dict[str, OperatorKeyAddresses]:
    return {chain_key: key_ring.addresses for chain_key, key_ring in operator_key_rings.items()}


def refuse_operator_keys_in_api_process() -> None:
    names = find_set_operator_key_variable_names()
    if names:
        raise RuntimeError(
            f"the API process must not hold operator keys, but {', '.join(names)} is set: blank it for the api "
            "service (docker-compose.yml)"
        )


async def open_invoice_wiring(
    exit_stack: AsyncExitStack,
    *,
    operator_key_addresses: dict[str, OperatorKeyAddresses],
) -> InvoiceWiring:
    """The chains' RPC, factory and Chainlink clients, ip-api, and the settings every invoice service reads. The API
    passes no operator key addresses (module docstring)."""
    chains_service = ChainsService(chains=read_enabled_chains())
    rpc_clients: dict[str, EvmRpcClient] = {}
    chainlink_clients: dict[str, ChainlinkApiClient] = {}
    # An EVM chain has its own RPC client and prices over it unless it names price endpoints (Amoy reads mainnet
    # feeds over its own). A Monero chain has no RPC client of its own (PLAN.md section 14): it prices over the enabled
    # EVM chain whose `chain_id` is its `price_chain_id` (Polygon's client, so Polygon's boot gains no second chain-id
    # check of its own URLs), and only with no such chain enabled over a client of its `price_rpc_urls`, shared by
    # every Monero chain that names the same price chain.
    for chain in chains_service.evm_chains:
        rpc_client = EvmRpcClient(chain_id=chain.chain_id, rpc_urls=chain.rpc_urls)
        exit_stack.push_async_callback(rpc_client.close)
        await rpc_client.verify_chain_id()
        rpc_clients[chain.key] = rpc_client
        price_rpc_client = rpc_client
        if chain.price_rpc_urls:
            price_rpc_client = EvmRpcClient(chain_id=chain.price_chain_id, rpc_urls=chain.price_rpc_urls)
            exit_stack.push_async_callback(price_rpc_client.close)
            await price_rpc_client.verify_chain_id()
        chainlink_clients[chain.key] = ChainlinkApiClient(rpc_client=price_rpc_client)
    monero_price_rpc_clients_by_chain_key: dict[str, EvmRpcClient] = {}
    for chain in chains_service.monero_chains:
        # The feeds are read over `price_chain_key`'s own client when that chain is enabled (one boot dependency,
        # not two), else over `price_rpc_urls`, shared by the Monero chains that name the same price chain.
        price_rpc_client = rpc_clients.get(chain.price_chain_key)
        if price_rpc_client is not None and price_rpc_client.chain_id != chain.price_chain_id:
            raise ValueError(
                f"chains.{chain.key}: price_chain_key {chain.price_chain_key} is not chain {chain.price_chain_id}"
            )
        if price_rpc_client is None:
            price_rpc_client = monero_price_rpc_clients_by_chain_key.get(chain.price_chain_key)
        if price_rpc_client is None:
            price_rpc_client = EvmRpcClient(chain_id=chain.price_chain_id, rpc_urls=chain.price_rpc_urls)
            exit_stack.push_async_callback(price_rpc_client.close)
            await price_rpc_client.verify_chain_id()
            monero_price_rpc_clients_by_chain_key[chain.price_chain_key] = price_rpc_client
        chainlink_clients[chain.key] = ChainlinkApiClient(rpc_client=price_rpc_client)
    ip_api_key = read_ip_api_settings().ip_api_key
    ip_api_client: IpApiClient | None = None
    if ip_api_key.get_secret_value():
        ip_api_http_client = await exit_stack.enter_async_context(httpx.AsyncClient())
        ip_api_client = IpApiClient(http_client=ip_api_http_client, api_key=ip_api_key)
    monero_sidecar = await open_monero_sidecar(exit_stack, chains_service=chains_service)
    invoice_settings = read_invoice_settings()
    return InvoiceWiring(
        chains_service=chains_service,
        rpc_clients=rpc_clients,
        factory_clients={
            chain.key: DepositFactoryApiClient(chain.factory_address)
            for chain in chains_service.evm_chains
            if chain.factory_address is not None
        },
        chainlink_clients=chainlink_clients,
        ip_api_client=ip_api_client,
        monero_sidecar=monero_sidecar,
        monero_private_view_key_encryption_key=derive_private_view_key_encryption_key(
            invoice_settings.invoice_salt_seed
        ),
        operator_key_addresses=operator_key_addresses,
        invoice_settings=invoice_settings,
        screening_settings=read_screening_settings(),
        public_base_url=read_application_settings().public_base_url.rstrip("/"),
        invoice_callback_url_check=check_invoice_callback_url,
        gateway_deposit_address_check=is_gateway_deposit_address,
    )


@asynccontextmanager
async def open_api_resources() -> AsyncGenerator[ApiResources]:
    refuse_operator_keys_in_api_process()
    async with AsyncExitStack() as exit_stack:
        database, redis_client = await open_store_resources(
            exit_stack,
            application_name="pay-api",
            pool_size=API_DATABASE_POOL_SIZE,
            side_transaction_pool_size=API_SIDE_TRANSACTION_POOL_SIZE,
        )
        invoice_wiring = await open_invoice_wiring(exit_stack, operator_key_addresses={})
        # Webhooks: no redirects (Standard Webhooks treats a 3xx as a failure) and no proxy from the environment. Each
        # request goes to the receiver's checked IP with its hostname as TLS SNI, and httpcore 1.0 reuses a pooled
        # connection by scheme, IP and port alone (``Connection.can_handle_request``), so a kept-alive connection to one
        # receiver could carry another's request on the same IP (two sites behind one CDN) without that hostname's
        # certificate check: webhooks keep no connection alive (one TLS handshake per attempt, at most 20 per 2 s pass).
        # Discord messages only ever go to one host, so its own client keeps them alive.
        webhook_http_client = await exit_stack.enter_async_context(
            httpx.AsyncClient(follow_redirects=False, trust_env=False, limits=httpx.Limits(max_keepalive_connections=0))
        )
        discord_http_client = await exit_stack.enter_async_context(
            httpx.AsyncClient(follow_redirects=False, trust_env=False)
        )
        yield ApiResources(
            database=database,
            redis_client=redis_client,
            rate_limiter=RateLimiter(redis_client.client),
            trusted_proxy_addresses=read_application_settings().trusted_proxy_addresses,
            chains_service=invoice_wiring.chains_service,
            invoice_wiring=invoice_wiring,
            # The chains the watcher watches (open_watcher_resources): EVM chains with a factory address, and Monero chains
            # while a sidecar is configured (with none they idle on purpose, MoneroChainsIdle).
            watcher_liveness_service=WatcherLivenessService(
                watched_chains=[
                    *(
                        chain
                        for chain in invoice_wiring.chains_service.evm_chains
                        if chain.key in invoice_wiring.factory_clients
                    ),
                    *(invoice_wiring.chains_service.monero_chains if monero_sidecar_is_configured() else []),
                ],
                redis=redis_client.client,
                session_factory=database.session_factory,
                operator_alert_account_id=invoice_wiring.invoice_settings.operator_alert_account_id,
            ),
            ip_api_key_alert_service=IpApiKeyAlertService(
                redis=redis_client.client,
                session_factory=database.session_factory,
                operator_alert_account_id=invoice_wiring.invoice_settings.operator_alert_account_id,
            ),
            account_webhook_delivery_service=AccountWebhookDeliveryService(
                session_factory=database.session_factory,
                webhook_http_client=webhook_http_client,
                discord_http_client=discord_http_client,
                redis=redis_client.client,
                encryption_key=derive_webhook_secret_encryption_key(invoice_wiring.invoice_settings.invoice_salt_seed),
                allow_private_hosts=read_webhook_allow_private_hosts(),
            ),
            account_event_stream_service=AccountEventStreamService(session_factory=database.session_factory),
        )


async def log_operator_account_problems(database: Database, *, operator_account_ids: tuple[int, ...]) -> None:
    """Once at watcher start: a WARNING when ``PAY_OPERATOR_ACCOUNT_IDS`` is empty, and one for each id it lists that
    no account has."""
    if not operator_account_ids:
        log.emit(logger, event=OperatorAccountNotConfigured(missing_setting="PAY_OPERATOR_ACCOUNT_IDS"))
        return
    async with database.session_factory() as session:
        missing_account_ids = await AccountService.on_session(session).list_missing_account_ids(operator_account_ids)
    for missing_account_id in missing_account_ids:
        log.emit(
            logger, event=OperatorAccountNotFound(account_id=missing_account_id, setting="PAY_OPERATOR_ACCOUNT_IDS")
        )


@asynccontextmanager
async def open_watcher_resources() -> AsyncGenerator[WatcherResources]:
    async with AsyncExitStack() as exit_stack:
        database, redis_client = await open_store_resources(
            exit_stack, application_name="pay-watcher", pool_size=WATCHER_DATABASE_POOL_SIZE
        )
        operator_key_rings = read_operator_key_rings()
        invoice_wiring = await open_invoice_wiring(exit_stack, operator_key_addresses=addresses_of(operator_key_rings))
        watched_chains = []
        for chain in invoice_wiring.chains_service.evm_chains:
            if chain.key not in invoice_wiring.factory_clients:
                log.emit(
                    logger,
                    event=ChainNotReadyForWatching(
                        chain_key=chain.key, missing_setting=f"PAY_FACTORY_ADDRESS_{chain.key.upper()}"
                    ),
                )
                continue
            if chain.key not in operator_key_rings:
                raise RuntimeError(f"PAY_OPERATOR_PRIVATE_KEY_{chain.key.upper()} is not set; the watcher signs sweeps")
            watched_chains.append(chain)

        monero_watched_chains = invoice_wiring.chains_service.monero_chains
        if monero_watched_chains and not monero_sidecar_is_configured():
            log.emit(
                logger,
                event=MoneroChainsIdle(
                    chain_keys=[chain.key for chain in monero_watched_chains],
                    missing_setting="PAY_MONERO_SIDECAR_URL",
                ),
            )
            monero_watched_chains = []
        await log_operator_account_problems(
            database, operator_account_ids=invoice_wiring.invoice_settings.operator_account_ids
        )

        async def apply_settlement_outcome_to_invoice(session: AsyncSession, *, outcome: SettlementOutcome) -> None:
            readings_service = ChainReadingsService.on_session(
                session, redis=redis_client.client, wiring=invoice_wiring
            )
            await readings_service.apply_settlement_outcomes([outcome])

        async def apply_static_deposit_sweep_outcome(
            session: AsyncSession, *, outcome: StaticDepositSweepOutcome
        ) -> None:
            readings_service = StaticDepositReadingsService.on_session(
                session, redis=redis_client.client, wiring=invoice_wiring
            )
            await readings_service.apply_sweep_outcome(outcome)

        settlement_runner = SettlementRunner(
            apply_settlement_outcome=apply_settlement_outcome_to_invoice,
            apply_static_deposit_sweep_outcome=apply_static_deposit_sweep_outcome,
            session_factory=database.session_factory,
            chains_service=invoice_wiring.chains_service,
            operator_key_rings=operator_key_rings,
            factory_clients=invoice_wiring.factory_clients,
            rpc_clients=invoice_wiring.rpc_clients,
        )
        yield WatcherResources(
            database=database,
            redis_client=redis_client,
            invoice_wiring=invoice_wiring,
            chain_watchers=[
                ChainWatcherService(
                    chain=chain,
                    rpc_client=invoice_wiring.rpc_clients[chain.key],
                    factory_client=invoice_wiring.factory_clients[chain.key],
                    settlement_runner=settlement_runner,
                    session_factory=database.session_factory,
                    redis=redis_client.client,
                    wiring=invoice_wiring,
                    operator_alert_service=OperatorAlertService(
                        chain=chain,
                        session_factory=database.session_factory,
                        redis=redis_client.client,
                        wiring=invoice_wiring,
                        settlement_runner=settlement_runner,
                        operator_key_maintenance_service=OperatorKeyMaintenanceService(
                            chain=chain,
                            key_addresses=operator_key_rings[chain.key].addresses,
                            session_factory=database.session_factory,
                            rpc_client=invoice_wiring.rpc_clients[chain.key],
                        ),
                    ),
                    deposit_rescan_service=DepositRescanService(
                        chain=chain,
                        session_factory=database.session_factory,
                        redis=redis_client.client,
                        wiring=invoice_wiring,
                    ),
                    static_deposit_scan_service=StaticDepositScanService(
                        chain=chain,
                        session_factory=database.session_factory,
                        redis=redis_client.client,
                        wiring=invoice_wiring,
                    ),
                )
                for chain in watched_chains
            ],
            monero_chain_watchers=[
                MoneroChainWatcherService(
                    chain=chain,
                    session_factory=database.session_factory,
                    redis=redis_client.client,
                    wiring=invoice_wiring,
                )
                for chain in monero_watched_chains
            ],
            scheduled_invoice_runner=ScheduledInvoiceRunner(
                session_factory=database.session_factory, redis=redis_client.client, wiring=invoice_wiring
            ),
        )


@asynccontextmanager
async def open_cli_invoice_resources() -> AsyncGenerator[CliInvoiceResources]:
    async with AsyncExitStack() as exit_stack:
        database, redis_client = await open_store_resources(
            exit_stack, application_name="pay-cli", pool_size=CLI_DATABASE_POOL_SIZE
        )
        yield CliInvoiceResources(
            database=database,
            redis_client=redis_client,
            invoice_wiring=await open_invoice_wiring(
                exit_stack, operator_key_addresses=addresses_of(read_operator_key_rings())
            ),
        )


@asynccontextmanager
async def open_cli_database() -> AsyncGenerator[Database]:
    """Postgres alone, with no Redis or RPC: ``record-operator-alert`` must work while either is down."""
    settings = read_application_settings()
    database = Database(
        settings.database_url,
        application_name="pay-cli",
        pool_size=CLI_DATABASE_POOL_SIZE,
        maximum_overflow=CLI_DATABASE_POOL_SIZE,
    )
    try:
        await database.assert_at_head()
        yield database
    finally:
        await database.close()


@asynccontextmanager
async def open_cli_chain_resources() -> AsyncGenerator[InvoiceWiring]:
    """The invoice wiring alone, with no Postgres or Redis: ``recover-deposit`` runs when the database is lost."""
    async with AsyncExitStack() as exit_stack:
        yield await open_invoice_wiring(exit_stack, operator_key_addresses=addresses_of(read_operator_key_rings()))


async def run_process_loop(
    run_pass: Callable[[], Awaitable[bool | None]], *, loop_name: str, interval_seconds: float
) -> None:
    """Run ``run_pass`` until cancelled, sleeping ``interval_seconds`` after each pass, except after a pass that
    returns False: more work is waiting, so the next starts at once. A pass that raises is logged; the loop goes on."""
    while True:
        # Broad on purpose: the loop is the process's only runner of this pass, so no failure may end it.
        try:
            more_work_waiting = await run_pass() is False
        except Exception:
            log.exception(logger, event=ProcessLoopPassFailed(loop_name=loop_name))
            more_work_waiting = False
        if not more_work_waiting:
            await asyncio.sleep(interval_seconds)


async def run_api_loops(resources: ApiResources) -> None:
    """The API process's background work, until cancelled: the watcher liveness and ip-api key checks once a minute,
    webhook delivery every 2 s, Discord message delivery beside it every 2 s and the account event streams' shared
    poller every 2 s while a stream is open."""
    async with asyncio.TaskGroup() as task_group:
        task_group.create_task(
            run_reported_loop(
                "account_event_stream_poller",
                run_loop=lambda: run_process_loop(
                    resources.account_event_stream_service.poll_subscribed_account_events,
                    loop_name="account_event_stream_poller",
                    interval_seconds=ACCOUNT_EVENT_POLL_INTERVAL_SECONDS,
                ),
            )
        )
        task_group.create_task(
            run_reported_loop(
                "watcher_liveness",
                run_loop=lambda: run_process_loop(
                    resources.watcher_liveness_service.check_watcher_heartbeats,
                    loop_name="watcher_liveness",
                    interval_seconds=WATCHER_LIVENESS_CHECK_INTERVAL_SECONDS,
                ),
            )
        )
        task_group.create_task(
            run_reported_loop(
                "ip_api_key_alert",
                run_loop=lambda: run_process_loop(
                    resources.ip_api_key_alert_service.check_ip_api_key_rejection,
                    loop_name="ip_api_key_alert",
                    interval_seconds=IP_API_KEY_CHECK_INTERVAL_SECONDS,
                ),
            )
        )
        task_group.create_task(
            run_reported_loop(
                "account_webhook_delivery",
                run_loop=lambda: run_process_loop(
                    resources.account_webhook_delivery_service.deliver_account_webhooks,
                    loop_name="account_webhook_delivery",
                    interval_seconds=WEBHOOK_DELIVERY_INTERVAL_SECONDS,
                ),
            )
        )
        task_group.create_task(
            run_reported_loop(
                "account_discord_delivery",
                run_loop=lambda: run_process_loop(
                    resources.account_webhook_delivery_service.deliver_account_discord_messages,
                    loop_name="account_discord_delivery",
                    interval_seconds=WEBHOOK_DELIVERY_INTERVAL_SECONDS,
                ),
            )
        )


async def run_reported_loop(loop_name: str, *, run_loop: Callable[[], Awaitable[None]]) -> None:
    """Run an endless loop and log the moment it dies."""
    try:
        await run_loop()
    except asyncio.CancelledError:
        raise
    except BaseException:
        log.exception(logger, event=ProcessLoopStopped(loop_name=loop_name))
        raise
