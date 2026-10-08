"""Timer for the call-retry scheduler; the work is ``RetryService.run_due_once``."""
import asyncio

from src.config.settings import get_settings
from src.scheduler.periodic import run_periodic
from src.services.retry_service import RetryService


async def retry_loop(stop: asyncio.Event) -> None:
    await run_periodic("Call-retry scheduler", stop, get_settings().retry_poll_seconds, lambda: RetryService().run_due_once())
