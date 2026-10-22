"""The watcher process: ``python -m app.watcher`` (PLAN.md section 5). It never imports FastAPI."""

import asyncio
import contextlib
import signal

from app.config import read_logging_settings
from app.lifespan.watcher_lifespan import run_watcher_process
from app.shared.logging import configure_logging


async def run_watcher_until_terminated() -> None:
    """Run the watcher until SIGTERM cancels it, so ``docker stop`` ends it through its cleanup instead of a SIGKILL
    after the stop grace period. The watcher is PID 1 in its container, and "A process running as PID 1 inside a
    container is treated specially by Linux: it ignores any signal with the default action"
    (https://docs.docker.com/reference/cli/docker/container/run/); ``loop.add_signal_handler`` lets the handler cancel
    the task from the event loop (https://docs.python.org/3/library/asyncio-eventloop.html#asyncio.loop.add_signal_handler).
    A cancelled settlement step is no worse than a killed one: each step commits or rolls back whole."""
    watcher_task = asyncio.create_task(run_watcher_process())
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, watcher_task.cancel)
    with contextlib.suppress(asyncio.CancelledError):
        await watcher_task


if __name__ == "__main__":
    configure_logging(read_logging_settings().log_level)
    asyncio.run(run_watcher_until_terminated())
