"""ASGI lifespan of the API process: open its resources, serve, close them.

It migrates nothing: the deploy runs ``alembic upgrade head`` first and ``open_api_resources`` refuses a schema
behind head. Request-scoped services come from each domain's ``dependencies.py``. While it serves, the API's own
loops run beside the requests (``run_api_loops``: the watcher liveness check, webhook delivery and the account event
streams' shared poller) and are cancelled on shutdown.
"""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from app.lifespan.composition import open_api_resources, run_api_loops
from app.lifespan.resources import ApiLifespanState
from app.log_events import ApplicationStarted, ApplicationStopped
from app.shared import logging as log

logger = log.get_logger(__name__)


@asynccontextmanager
async def lifespan(_application: FastAPI) -> AsyncGenerator[ApiLifespanState]:
    async with open_api_resources() as resources:
        api_loops_task = asyncio.create_task(run_api_loops(resources))
        log.emit(logger, event=ApplicationStarted())
        try:
            yield {"resources": resources}
        finally:
            api_loops_task.cancel()
            with suppress(asyncio.CancelledError):
                await api_loops_task
    log.emit(logger, event=ApplicationStopped())
