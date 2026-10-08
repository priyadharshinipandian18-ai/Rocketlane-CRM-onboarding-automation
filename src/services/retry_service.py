"""Resumes tier runs whose next AE call attempt is due.

A LangGraph run can't sleep for 30 minutes, so ``schedule_call_retry`` /
``place_plan_tier_call`` (deferred) park the run with ``status='waiting'``,
``waiting_for='call_retry'`` and ``due_at``. This service claims due rows atomically
(``FOR UPDATE SKIP LOCKED``) and resumes each with ``{"action": "retry"}`` through the
orchestrator — safe even with several app instances.
"""
import logging

from src.services import WorkflowRunService
from src.services.workflow_orchestrator import WorkflowOrchestrator

log = logging.getLogger(__name__)


class RetryService:
    def __init__(self, orchestrator: WorkflowOrchestrator | None = None) -> None:
        self.workflow_runs = WorkflowRunService()
        self.orchestrator = orchestrator or WorkflowOrchestrator()

    async def run_due_once(self) -> int:
        """Claim and resume every due run. Returns how many were claimed."""
        due = await self.workflow_runs.claim_due_call_retries()
        for run in due:
            log.info("Call retry due — resuming onboarding %s", run.onboarding_id)
            await self.orchestrator.resume(run, {"action": "retry"})
        return len(due)
