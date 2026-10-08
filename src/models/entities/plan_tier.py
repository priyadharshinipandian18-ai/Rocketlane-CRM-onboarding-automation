from datetime import datetime

from src.models.base_model import BaseModel
from src.models.enums import CsmModel, PlanTierCode


class PlanTier(BaseModel):
    """Configuration that drives every per-tier decision (template, timeline,
    CSM model, Slack naming/tone). The LLM never picks these — it only ever
    produces a tier ``code``."""

    __table_name__ = "plan_tiers"
    __primary_key__ = "code"

    code: PlanTierCode
    display_name: str
    rocketlane_template_id: str
    duration_days: int
    csm_model: CsmModel
    slack_channel_prefix: str
    welcome_message_template: str
    updated_at: datetime | None = None
