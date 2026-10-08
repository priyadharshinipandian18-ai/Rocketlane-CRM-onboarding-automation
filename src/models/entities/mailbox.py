from datetime import datetime

from src.models.base_model import BaseModel


class Mailbox(BaseModel):
    """The CS-team inbox being monitored, plus its Gmail sync cursor."""

    __table_name__ = "mailboxes"

    id: str | None = None
    email: str
    history_id: int | None = None
    watch_expiration: datetime | None = None
    last_synced_at: datetime | None = None
    updated_at: datetime | None = None
