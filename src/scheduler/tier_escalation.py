"""Timer for the "AE didn't answer the escalation email" clock; the work is
``TierEscalationService.run_due_once`` (stop the run after HUMAN_STOP_AFTER_HOURS)."""
import asyncio

from src.config.settings import get_settings
from src.scheduler.periodic import run_periodic
from src.services.tier_escalation_service import TierEscalationService


async def tier_escalation_loop(stop: asyncio.Event) -> None:
    await run_periodic(
        "AE-reply timeout", stop, get_settings().escalation_poll_seconds, lambda: TierEscalationService().run_due_once()
    )
