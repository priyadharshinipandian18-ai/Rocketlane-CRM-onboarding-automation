"""Polling mode: fetch AE-call results from Bolna instead of waiting for a webhook.

Only the timer lives here; the work is ``CallResultService.poll_in_flight_once``.
Needs only outbound HTTPS to Bolna — no tunnel, no public URL — and is safe
alongside the webhook: whichever sees a finished call first processes it, the
other is a no-op.
"""
import asyncio
import logging

from src.config.settings import get_settings
from src.scheduler.periodic import run_periodic
from src.services.call_result_service import CallResultService

log = logging.getLogger(__name__)


async def call_poll_loop(stop: asyncio.Event) -> None:
    settings = get_settings()
    if not settings.bolna_poll_enabled:
        log.info("Call-result polling disabled (BOLNA_POLL_ENABLED=false) — relying on the webhook")
        return
    await run_periodic("Call-result poller", stop, settings.bolna_poll_seconds, lambda: CallResultService().poll_in_flight_once())
