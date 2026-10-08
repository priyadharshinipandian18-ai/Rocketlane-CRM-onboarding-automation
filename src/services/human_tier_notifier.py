"""The escalation email: when the call couldn't confirm the plan tier, ask the AE by email.

``open_request`` (used by the tier workflow's ``request_ae_reply`` node) records an
``escalations`` row and sends the AE "reply ENTERPRISE or GROWTH". Free of the
graph and the orchestrator, so graph nodes can import it. A failed send is
logged + audited and never raises — the run still waits (and an operator can
answer through the API).
"""
import logging
from datetime import datetime, timedelta, timezone

from src.agents.tier_confirmation.tier_reply import clarification_email, make_token, tier_request_email
from src.config.settings import get_settings
from src.models.entities import Escalation
from src.services import (
    AccountExecutiveService,
    AuditEventService,
    OnboardingService,
    WorkflowRunService,
)
from src.services.email_service import EmailService, SentEmail
from src.services.escalation_service import EscalationService

log = logging.getLogger(__name__)


class HumanTierNotifier:
    def __init__(
        self,
        escalations: EscalationService | None = None,
        email: EmailService | None = None,
        onboardings: OnboardingService | None = None,
        account_executives: AccountExecutiveService | None = None,
        workflow_runs: WorkflowRunService | None = None,
        audit: AuditEventService | None = None,
    ) -> None:
        self.escalations = escalations or EscalationService()
        self.email = email or EmailService()
        self.onboardings = onboardings or OnboardingService()
        self.account_executives = account_executives or AccountExecutiveService()
        self.workflow_runs = workflow_runs or WorkflowRunService()
        self.audit = audit or AuditEventService()

    @staticmethod
    def stop_at() -> datetime:
        """When to stop waiting for the AE's reply (the run stays resumable)."""
        return datetime.now(timezone.utc) + timedelta(hours=get_settings().human_stop_after_hours)

    async def context(self, onboarding_id: str) -> tuple[str, str, str | None]:
        """(customer name, AE name, AE email) for an onboarding."""
        onboarding = await self.onboardings.get_by_id(onboarding_id)
        ae = await self.account_executives.get_by_id(onboarding.ae_id) if onboarding and onboarding.ae_id else None
        return (onboarding.customer_name if onboarding else "the customer", ae.name if ae else "", ae.email if ae else None)

    async def open_request(
        self, onboarding_id: str, ae_email: str | None, ae_name: str, customer_name: str, reason: str
    ) -> Escalation | None:
        """Record the escalation and email the AE. ``None`` when there is no AE address to
        write to (the run then waits for an operator)."""
        if not ae_email:
            log.warning("No AE email for onboarding %s — cannot send the escalation; waiting for an operator", onboarding_id)
            await self.audit.log_event(onboarding_id, "intake_agent", "human_notify_skipped", {"reason": "no AE email"})
            return None
        run = await self.workflow_runs.get_for(onboarding_id, "tier_confirmation")
        escalation = await self.escalations.create_attempt(onboarding_id, run.id, make_token(onboarding_id), ae_email)

        subject, body = tier_request_email(escalation.token, ae_name, customer_name, reason)
        sent = await self._send(onboarding_id, ae_email, subject, body, None, "human_tier_requested")
        if sent is None:
            await self.escalations.mark_failed(escalation.id)
        else:
            await self.escalations.set_sent(escalation.id, sent.message_id, sent.thread_id)
        return escalation

    async def send_clarification(self, escalation: Escalation, to: str, customer_name: str) -> None:
        subject, body = clarification_email(escalation.token, customer_name)
        await self._send(escalation.onboarding_id, to, subject, body, escalation.gmail_thread_id, "tier_reply_clarification_sent")

    async def _send(self, onboarding_id: str, to: str, subject: str, body: str, thread_id: str | None, event: str) -> SentEmail | None:
        if not get_settings().human_email_enabled:
            log.warning("HUMAN_EMAIL_ENABLED=false — not emailing %s: %s", to, subject)
            return None
        try:
            sent = await self.email.send(to, subject, body, thread_id)
        except Exception as exc:
            log.exception("Could not email %s (%s)", to, subject)
            await self.audit.log_event(onboarding_id, "intake_agent", "human_notify_failed", {"to": to, "subject": subject, "error": str(exc)[:300]})
            return None
        await self.audit.log_event(onboarding_id, "intake_agent", event, {"to": to, "subject": subject})
        return sent
