from pydantic import Field

from src.models.base_model import BaseModel


class DealStatusAnalysis(BaseModel):
    """Structured output of the analyze_deal_status LLM node."""

    is_deal_closed: bool = Field(
        ...,
        description="True only if the email reports a deal that has ACTUALLY closed (signed/won) — not a forecast, negotiation update, or inquiry.",
    )
    confidence_score: float = Field(..., description="How certain you are, from 0.0 (guess) to 1.0 (unambiguous).")
    closing_evidence: list[str] = Field(
        default_factory=list,
        description="Exact, verbatim quotes from the email that show the deal closed. Empty if it did not close. Never paraphrase.",
    )
    reasoning: str = Field(..., description="One or two sentences on why you reached this verdict.")
