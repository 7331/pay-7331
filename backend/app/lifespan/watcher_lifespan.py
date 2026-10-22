"""The watcher process: one loop per enabled chain and the invoice schedules' loop (PLAN.md section 18), under a
Postgres advisory lock so a second copy idles (PLAN.md section 2)."""

import asyncio

from app.lifespan.composition import open_watcher_resources, run_process_loop, run_reported_loop
from app.lifespan.resources import WatcherResources
from app.log_events import WatcherProcessStarted, WatcherProcessStopped
from app.payments.chain_watcher.log_events import WatcherLockHeldElsewhere
from app.payments.invoice_schedules.runner_service import SCHEDULED_INVOICE_PASS_INTERVAL_SECONDS
from app.shared import logging as log

logger = log.get_logger(__name__)

# pg_try_advisory_lock key: "pay-7331 watcher".
WATCHER_ADVISORY_LOCK_KEY = 7331_0001
LOCK_RETRY_SECONDS = 10


async def run_watcher_loops(resources: WatcherResources) -> None:
    """Runs until cancelled. With no chain to watch (say, Monero chains left idle with no sidecar) the schedules' loop
    still runs, so the process stays up rather than exit, which a restart policy would turn into a loop of starts."""
    async with asyncio.TaskGroup() as task_group:
        task_group.create_task(
            run_reported_loop(
                "scheduled_invoices",
                run_loop=lambda: run_process_loop(
                    resources.scheduled_invoice_runner.create_due_scheduled_invoices,
                    loop_name="scheduled_invoices",
                    interval_seconds=SCHEDULED_INVOICE_PASS_INTERVAL_SECONDS,
                ),
            )
        )
        for chain_watcher in resources.chain_watchers:
            loop_name = f"chain_watcher_{chain_watcher.chain.key}"
            task_group.create_task(
                run_reported_loop(
                    loop_name,
                    run_loop=lambda chain_watcher=chain_watcher, loop_name=loop_name: run_process_loop(
                        chain_watcher.run_tick,
                        loop_name=loop_name,
                        interval_seconds=chain_watcher.chain.poll_interval_seconds,
                    ),
                )
            )
        for monero_chain_watcher in resources.monero_chain_watchers:
            loop_name = f"monero_chain_watcher_{monero_chain_watcher.chain.key}"
            task_group.create_task(
                run_reported_loop(
                    loop_name,
                    run_loop=lambda monero_chain_watcher=monero_chain_watcher, loop_name=loop_name: run_process_loop(
                        monero_chain_watcher.run_tick,
                        loop_name=loop_name,
                        interval_seconds=monero_chain_watcher.chain.poll_interval_seconds,
                    ),
                )
            )


async def run_watcher_process() -> None:
    async with open_watcher_resources() as resources:
        while True:
            async with resources.database.hold_advisory_lock(WATCHER_ADVISORY_LOCK_KEY) as lock_taken:
                if lock_taken:
                    log.emit(
                        logger,
                        event=WatcherProcessStarted(
                            chain_keys=[watcher.chain.key for watcher in resources.chain_watchers]
                        ),
                    )
                    try:
                        await run_watcher_loops(resources)
                    finally:
                        log.emit(logger, event=WatcherProcessStopped())
                    return
            log.emit(logger, event=WatcherLockHeldElsewhere())
            await asyncio.sleep(LOCK_RETRY_SECONDS)
