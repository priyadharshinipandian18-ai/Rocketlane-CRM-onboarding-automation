"""The one loop every background timer uses: run ``tick`` every ``interval`` seconds until ``stop``.

A tick that raises never kills the loop. Failures are reported through a ``FailureTracker`` — a full
traceback the first time, a one-line summary every few minutes after that, a "recovered" line when it
works again — and the wait before the next tick grows (exponential backoff) while it keeps failing.
"""
import asyncio
import logging
from typing import Awaitable, Callable

from src.utils.failure_tracker import FailureTracker

log = logging.getLogger(__name__)


async def run_periodic(
    name: str,
    stop: asyncio.Event,
    interval: float,
    tick: Callable[[], Awaitable[object]],
    failures: FailureTracker | None = None,
) -> None:
    failures = failures or FailureTracker(name, logging.getLogger(f"src.scheduler.{name}"))
    log.info("%s started (every %ss)", name, interval)
    while not stop.is_set():
        wait = interval
        try:
            await tick()
        except Exception as exc:
            wait = max(interval, failures.failed(exc))
        else:
            failures.succeeded()
        try:
            await asyncio.wait_for(stop.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass
    log.info("%s stopped", name)
