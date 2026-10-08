from datetime import datetime

from src.models.base_model import BaseModel
from src.models.enums import EscalationStatus, PlanTierCode


class Escalation(BaseModel):
    """One "please tell us the plan tier" email sent to the AE after the call
    couldn't confirm it. A reply is matched back by the Gmail thread of the
    email we sent (or the ``token`` in its subject); only ``to_email`` — the AE
    — may answer."""

    __table_name__ = "escalations"

    id: str | None = None
    onboarding_id: str
    workflow_run_id: str
    attempt_no: int = 1
    token: str
    to_email: str
    gmail_message_id: str | None = None
    gmail_thread_id: str | None = None
    status: EscalationStatus = EscalationStatus.SENT
    sent_at: datetime | None = None
    answered_tier: PlanTierCode | None = None
    answered_by: str | None = None
    answered_inbound_email_id: str | None = None
    answered_at: datetime | None = None
