import json
from datetime import datetime
from typing import Any

from src.models.entities import InboundEmail
from src.models.enums import EmailKind, EmailKindSource, InboundEmailStatus
from src.services.base_service import BaseService


class InboundEmailService(BaseService[InboundEmail]):
    def __init__(self) -> None:
        super().__init__(InboundEmail)

    async def try_insert(
        self,
        mailbox_id: str,
        gmail_message_id: str,
        gmail_thread_id: str | None,
        from_address: str,
        subject: str,
        received_at: datetime,
        body_redacted: str | None = None,
    ) -> InboundEmail | None:
        """Insert the email, or return ``None`` if already seen.

        ``UNIQUE (mailbox_id, gmail_message_id)`` is what makes this safe
        against duplicate Pub/Sub notifications and replayed history — a
        second attempt to process the same Gmail message can never start a
        second onboarding. ``body_redacted`` must already be PII-redacted.
        """
        return await self.fetch_one(
            """
            INSERT INTO app.inbound_emails
                (mailbox_id, gmail_message_id, gmail_thread_id, from_address, subject, received_at, body_redacted)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (mailbox_id, gmail_message_id) DO NOTHING
            RETURNING *
            """,
            (mailbox_id, gmail_message_id, gmail_thread_id, from_address, subject, received_at, body_redacted),
        )

    async def update_classification(
        self,
        email_id: str,
        is_deal_email: bool,
        classification: dict[str, Any],
        status: InboundEmailStatus | str,
        kind: EmailKind | str | None = None,
        kind_source: EmailKindSource | str | None = None,
        escalation_id: str | None = None,
    ) -> None:
        """Record the outcome. ``kind`` / ``kind_source`` / ``escalation_id`` are only
        overwritten when given, so a later step can add the escalation outcome."""
        await self.execute(
            """
            UPDATE app.inbound_emails
            SET is_deal_email = %s, classification = %s, status = %s,
                kind = COALESCE(%s, kind), kind_source = COALESCE(%s, kind_source),
                escalation_id = COALESCE(%s, escalation_id)
            WHERE id = %s
            """,
            (
                is_deal_email,
                json.dumps(classification),
                InboundEmailStatus(status).value,
                EmailKind(kind).value if kind else None,
                EmailKindSource(kind_source).value if kind_source else None,
                escalation_id,
                email_id,
            ),
        )
