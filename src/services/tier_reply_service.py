"""Acts on an email the classifier recognised as a reply to our escalation.

Only the AE the escalation was sent to may answer, and only with one clear word.
A good answer claims the paused tier run and **resumes** it through the
orchestrator — the graph isn't re-run: it is parked at ``await_ae_reply`` in the
Postgres checkpointer (``thread_id`` ``tier:<onboarding_id>``) and continues from there.
"""
import logging
from email.utils import parseaddr

from src.agents.tier_confirmation.tier_reply import parse_tier
from src.services import AuditEventService, EscalationService, WorkflowRunService
from src.services.human_tier_notifier import HumanTierNotifier
from src.services.workflow_orchestrator import WorkflowOrchestrator

log = logging.getLogger(__name__)

MAX_CLARIFICATIONS = 2
"""Stops an auto-responder (out-of-office) and us nudging each other forever."""

UNKNOWN_ESCALATION = "unknown_escalation"
UNAUTHORIZED = "unauthorized_sender"
UNCLEAR = "unclear"
NOT_WAITING = "not_waiting"
RESUMED = "resumed"


class TierReplyService:
    def __init__(
        self,
        escalations: EscalationService | None = None,
        workflow_runs: WorkflowRunService | None = None,
        notifier: HumanTierNotifier | None = None,
        audit: AuditEventService | None = None,
        orchestrator: WorkflowOrchestrator | None = None,
    ) -> None:
        self.escalations = escalations or EscalationService()
        self.workflow_runs = workflow_runs or WorkflowRunService()
        self.notifier = notifier or HumanTierNotifier()
        self.audit = audit or AuditEventService()
        self.orchestrator = orchestrator or WorkflowOrchestrator()

    async def deliver_reply(self, escalation_id: str, inbound_email_id: str, sender: str, text: str) -> str:
        escalation = await self.escalations.get_by_id(escalation_id)
        if escalation is None:
            return UNKNOWN_ESCALATION
        onboarding_id = escalation.onboarding_id
        sender_address = parseaddr(sender)[1].lower()

        if sender_address != escalation.to_email.lower():
            log.warning("Ignoring plan-tier reply from %r for onboarding %s — only %s may answer",
                        sender_address, onboarding_id, escalation.to_email)
            await self.audit.log_event(onboarding_id, "intake_agent", "tier_reply_rejected", {"from": sender_address})
            return UNAUTHORIZED

        tier = parse_tier(text)
        if tier is None:
            await self._ask_again(escalation, sender_address)
            return UNCLEAR

        run = await self.workflow_runs.claim_for_tier_answer(onboarding_id)
        if run is None:
            log.info("Reply for onboarding %s ignored — it is not waiting for a tier any more", onboarding_id)
            return NOT_WAITING

        await self.audit.log_event(onboarding_id, f"human:{sender_address}", "tier_reply_accepted", {"plan_tier": tier})
        await self.orchestrator.resume(
            run, {"plan_tier": tier, "reviewer": sender_address, "inbound_email_id": inbound_email_id}
        )
        return RESUMED

    async def _ask_again(self, escalation, sender: str) -> None:
        sent = await self.audit.find_all(onboarding_id=escalation.onboarding_id, event_type="tier_reply_clarification_sent")
        if len(sent) >= MAX_CLARIFICATIONS:
            log.info("Not asking onboarding %s again — already sent %d clarifications", escalation.onboarding_id, len(sent))
            return
        customer, _, _ = await self.notifier.context(escalation.onboarding_id)
        await self.notifier.send_clarification(escalation, sender, customer)
