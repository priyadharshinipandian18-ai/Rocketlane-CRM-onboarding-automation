from datetime import datetime
from typing import Any

from src.models.base_model import BaseModel


class AuditEvent(BaseModel):
    """Append-only timeline of everything that happened to an onboarding."""

    __table_name__ = "audit_events"

    id: int | None = None
    onboarding_id: str | None = None
    actor: str
    event_type: str
    payload: dict[str, Any] | None = None
    created_at: datetime | None = None
