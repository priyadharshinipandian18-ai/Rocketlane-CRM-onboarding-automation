from datetime import date, datetime
from typing import Any

from src.models.base_model import BaseModel
from src.models.enums import RocketlaneProjectStatus


class RocketlaneProject(BaseModel):
    """Link to the created project. A ``creating`` row is written *before* the
    API call so a crash in between can't produce a duplicate project."""

    __table_name__ = "rocketlane_projects"

    id: str | None = None
    onboarding_id: str
    rocketlane_project_id: str | None = None
    template_id: str
    project_url: str | None = None
    owner_user_id: str | None = None
    manager_user_id: str | None = None
    start_date: date | None = None
    due_date: date | None = None
    status: RocketlaneProjectStatus = RocketlaneProjectStatus.CREATING
    request_payload: dict[str, Any] | None = None
    error: str | None = None
    created_at: datetime | None = None
