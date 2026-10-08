from src.models.base_model import BaseModel
from src.models.enums import CsRole, PlanTierCode


class CsTeamMember(BaseModel):
    """A CSM or CS manager — becomes the Rocketlane Project Owner / Manager."""

    __table_name__ = "cs_team_members"

    id: str | None = None
    name: str
    email: str
    role: CsRole
    pool: PlanTierCode | None = None
    rocketlane_user_id: str
    slack_user_id: str | None = None
    active: bool = True
