from typing import Literal

from pydantic import Field

from src.models.base_model import BaseModel


class DealEmailExtraction(BaseModel):
    """Structured output of the deal-email parser."""

    is_deal_closed: bool = Field(
        ..., description="True only if the email reports a deal that has actually closed/signed — not a forecast, inquiry, or negotiation update."
    )
    confidence: Literal["low", "medium", "high"] = Field(...)
    customer_name: str = Field(default="")
    customer_contact_name: str = Field(default="")
    customer_contact_email: str = Field(default="")
    ae_name: str = Field(default="", description="The Account Executive who sent/owns this deal.")
    ae_email: str = Field(default="")
    sf_opportunity_id: str = Field(default="", description="Salesforce opportunity ID, if mentioned. Empty if not present.")
    sf_opportunity_url: str = Field(default="", description="Salesforce opportunity link, if mentioned. Empty if not present.")
    deal_summary: str = Field(default="", description="One sentence describing the deal.")
    reasoning: str = Field(..., description="Why this was/wasn't classified as a closed deal.")
