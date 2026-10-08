from datetime import datetime
from typing import Any

from src.models.base_model import BaseModel
from src.models.enums import EmailKind, EmailKindSource, InboundEmailStatus


class InboundEmail(BaseModel):
    """Every email the intake agent looked at. ``UNIQUE (mailbox_id,
    gmail_message_id)`` is what makes a replayed Pub/Sub notification a no-op.

    Only the PII-redacted body is stored (``body_redacted``) — the raw body
    never reaches the database or the LangGraph checkpoints.
    """

    __table_name__ = "inbound_emails"

    id: str | None = None
    mailbox_id: str
    gmail_message_id: str
    gmail_thread_id: str | None = None
    from_address: str | None = None
    subject: str | None = None
    received_at: datetime | None = None
    body_redacted: str | None = None
    is_deal_email: bool | None = None
    kind: EmailKind | None = None
    kind_source: EmailKindSource | None = None
    classification: dict[str, Any] | None = None
    status: InboundEmailStatus = InboundEmailStatus.PENDING
    escalation_id: str | None = None
    created_at: datetime | None = None
