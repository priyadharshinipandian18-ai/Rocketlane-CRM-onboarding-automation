from datetime import date, datetime
from typing import Any

from src.models.base_model import BaseModel
from src.models.enums import OnboardingStatus, PlanTierCode, PlanTierSource


class Onboarding(BaseModel):
    """The central record — one per closed deal."""

    __table_name__ = "onboardings"

    id: str | None = None
    inbound_email_id: str
    customer_name: str
    customer_contact_name: str | None = None
    customer_contact_email: str
    customer_domain: str | None = None
    ae_id: str | None = None
    sf_opportunity_url: str | None = None
    sf_opportunity_id: str | None = None
    extra_details: dict[str, Any] | None = None
    plan_tier: PlanTierCode | None = None
    plan_tier_source: PlanTierSource | None = None
    plan_confirmed_at: datetime | None = None
    csm_id: str | None = None
    manager_id: str | None = None
    start_date: date | None = None
    target_go_live_date: date | None = None
    status: OnboardingStatus = OnboardingStatus.RECEIVED
    status_reason: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
