"""Timer that starts ``pending`` workflow runs.

A hand-off between workflows (tier → provisioning) is recorded as a ``pending`` run
*before* anything starts it, so a crash between the two can't lose it. The orchestrator
normally starts it straight away; this sweeper is the safety net that picks up whatever
was left pending (``WorkflowOrchestrator.start_pending``).
"""
import asyncio

from src.config.settings import get_settings
from src.scheduler.periodic import run_periodic
from src.services.workflow_orchestrator import WorkflowOrchestrator


async def workflow_sweeper_loop(stop: asyncio.Event) -> None:
    await run_periodic(
        "Workflow sweeper", stop, get_settings().workflow_sweep_seconds, lambda: WorkflowOrchestrator().start_pending()
    )
