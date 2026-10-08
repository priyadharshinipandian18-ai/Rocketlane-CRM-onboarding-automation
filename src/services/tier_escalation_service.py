"""The clock behind "the AE didn't answer the escalation email": stop the run after a
timeout — and bring a stopped run back.

*Stopping* loses nothing. The tier run is parked at ``await_ae_reply`` in the LangGraph
checkpointer (Postgres, keyed by ``thread_id``); ``stopped`` only takes it off the
"actively waiting" list. A late reply from the AE, or ``POST /onboardings/{id}/plan-tier``,
continues it from exactly that node.
"""
import logging

from src.config.settings import get_settings
from src.models.enums import OnboardingStatus
from src.services import AuditEventService, EscalationService, OnboardingService, WorkflowRunService
from src.services.human_tier_notifier import HumanTierNotifier

log = logging.getLogger(__name__)


class TierEscalationService:
    def __init__(
        self,
        workflow_runs: WorkflowRunService | None = None,
        onboardings: OnboardingService | None = None,
        escalations: EscalationService | None = None,
        notifier: HumanTierNotifier | None = None,
        audit: AuditEventService | None = None,
    ) -> None:
        self.workflow_runs = workflow_runs or WorkflowRunService()
        self.onboardings = onboardings or OnboardingService()
        self.escalations = escalations or EscalationService()
        self.notifier = notifier or HumanTierNotifier()
        self.audit = audit or AuditEventService()

    async def run_due_once(self) -> int:
        """Stop every tier run whose wait for the AE's reply has timed out. Returns how many."""
        hours = get_settings().human_stop_after_hours
        stopped = await self.workflow_runs.claim_due_stops()
        for run in stopped:
            reason = (
                f"No plan tier from the AE within {hours:g} hours — run stopped (nothing lost). The AE can still reply "
                f"to the email, or POST /onboardings/{run.onboarding_id}/resume to ask again."
            )
            await self.onboardings.update_status(run.onboarding_id, OnboardingStatus.NEEDS_HUMAN_REVIEW, reason)
            await self.audit.log_event(run.onboarding_id, "intake_agent", "human_wait_stopped", {"after_hours": hours})
            log.warning("%s onboarding=%s", reason, run.onboarding_id)
        return len(stopped)

    async def reopen(self, onboarding_id: str) -> bool:
        """Bring a *stopped* run back to waiting for the AE: fresh deadline, and ask again
        (a new escalation attempt). ``False`` if the run isn't stopped."""
        run = await self.workflow_runs.reopen_stopped(onboarding_id, self.notifier.stop_at())
        if run is None:
            return False
        customer, ae_name, ae_email = await self.notifier.context(onboarding_id)
        previous = await self.escalations.latest_for(onboarding_id)
        # The request remembers who was asked even when that AE isn't in app.account_executives.
        await self.notifier.open_request(
            onboarding_id, (previous.to_email if previous else ae_email), ae_name, customer,
            "the earlier request timed out and the run has been resumed",
        )
        await self.onboardings.update_status(onboarding_id, OnboardingStatus.NEEDS_HUMAN_REVIEW, "Resumed — waiting for a plan tier")
        await self.audit.log_event(onboarding_id, "operator", "human_wait_resumed", {})
        return True
