from typing import Literal

from src.models.base_model import BaseModel


class NodeReport(BaseModel):
    """Shared reasoning envelope every node appends to the run's trail,
    mirroring rappit-ai-companion's NodeReport convention."""

    producer: str
    status: Literal[
        "SUCCESS",
        "DUPLICATE",
        "NOT_A_DEAL",
        "SECURITY_REJECTED",
        "CHANNEL_CREATED",
        "SKIPPED",
        "AWAITING_VOICE_CALL",
        "CALL_DEFERRED",
        "VOICE_CALL_FAILED",
        "TIER_CONFIRMED",
        "TIER_NOT_CONFIRMED",
        "RETRY_SCHEDULED",
        "NEEDS_HUMAN",
        "RESOLVED",
        "FAILED",
    ]
    reasoning: str
