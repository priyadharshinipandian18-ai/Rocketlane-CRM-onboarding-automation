"""Response bodies for the HTTP API."""
from datetime import date, datetime

from src.models.base_model import BaseModel
from src.models.entities import Onboarding
from src.models.enums import OnboardingStatus, PlanTierCode, WaitingFor, WorkflowName, WorkflowRunStatus


class HealthResponse(BaseModel):
    status: str = "ok"


class PlanTierResumeResponse(BaseModel):
    status: str = "resuming"
    onboarding_id: str
    plan_tier: PlanTierCode


class ResumeResponse(BaseModel):
    status: str
    """``resuming`` (a tier was supplied) or ``reopened`` (the AE is being asked again)."""
    onboarding_id: str
    plan_tier: PlanTierCode | None = None


class GmailWatchResponse(BaseModel):
    """Result of registering / renewing the Gmail push notifications."""

    mailbox_email: str
    topic: str
    history_id: int
    watch_expiration: datetime


class OnboardingSummary(BaseModel):
    """What the API shows about an onboarding — not the raw table row."""

    id: str
    customer_name: str
    customer_contact_email: str
    plan_tier: PlanTierCode | None = None
    status: OnboardingStatus
    status_reason: str | None = None
    target_go_live_date: date | None = None
    created_at: datetime | None = None

    @classmethod
    def from_entity(cls, onboarding: Onboarding) -> "OnboardingSummary":
        return cls.model_validate(onboarding)


class WorkflowRunSummary(BaseModel):
    """One workflow run of an onboarding: where it is, why it waits, when it times out."""

    workflow: WorkflowName
    thread_id: str
    status: WorkflowRunStatus
    waiting_for: WaitingFor | None = None
    current_node: str | None = None
    due_at: datetime | None = None
    stop_at: datetime | None = None
    outcome: str | None = None
    attempt_count: int = 0
    last_error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class RetryResponse(BaseModel):
    status: str = "retrying"
    onboarding_id: str
    workflow: WorkflowName


class PubSubAckResponse(BaseModel):
    status: str = "processed"
