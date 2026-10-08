"""Request bodies for the HTTP API."""
from pydantic import model_validator

from src.models.base_model import BaseModel
from src.models.enums import PlanTierCode


class ManualPlanTierRequest(BaseModel):
    """CS manager sets the plan tier by hand after the call attempts are
    exhausted (or the AE has no phone on file)."""

    plan_tier: PlanTierCode
    reviewer: str
    """Email of the person confirming the tier (recorded in the audit trail)."""


class ResumeOnboardingRequest(BaseModel):
    """Continue a run that is waiting for / was stopped waiting for a plan tier.

    With ``plan_tier`` (+ ``reviewer``): supply the answer and carry on.
    Without: re-open the request — restart the clock and email the AE again.
    """

    plan_tier: PlanTierCode | None = None
    reviewer: str | None = None

    @model_validator(mode="after")
    def _reviewer_required_with_a_tier(self) -> "ResumeOnboardingRequest":
        if self.plan_tier and not self.reviewer:
            raise ValueError("reviewer is required when plan_tier is given")
        return self
