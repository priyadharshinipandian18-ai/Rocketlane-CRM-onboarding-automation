from datetime import datetime
from typing import Any

from src.models.base_model import BaseModel
from src.models.enums import PlanTierCode, VoiceCallOutcome, VoiceCallStatus


class VoiceCall(BaseModel):
    """One call attempt to the AE. ``provider_call_id`` is how the async
    result finds its way back to the right onboarding."""

    __table_name__ = "voice_calls"

    id: str | None = None
    onboarding_id: str
    attempt_no: int
    provider: str
    provider_call_id: str | None = None
    to_phone: str
    status: VoiceCallStatus = VoiceCallStatus.QUEUED
    outcome: VoiceCallOutcome | None = None
    readback_confirmed: bool = False
    ended_reason: str | None = None
    notes: str | None = None
    detected_plan_tier: PlanTierCode | None = None
    confidence: float | None = None
    transcript: str | None = None
    recording_url: str | None = None
    duration_seconds: int | None = None
    raw_webhook: dict[str, Any] | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    created_at: datetime | None = None
