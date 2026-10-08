from datetime import datetime

from src.models.entities import Mailbox
from src.services.base_service import BaseService


class MailboxService(BaseService[Mailbox]):
    def __init__(self) -> None:
        super().__init__(Mailbox)

    async def get_or_create(self, email: str) -> Mailbox:
        found = await self.find_one(email=email)
        return found or await self.save(Mailbox(email=email))

    async def update_cursor(self, mailbox_id: str, history_id: int, watch_expiration: datetime | None = None) -> None:
        """Advance the Gmail sync cursor; an omitted ``watch_expiration`` keeps the stored one."""
        await self.execute(
            """
            UPDATE app.mailboxes
            SET history_id = %s,
                watch_expiration = COALESCE(%s, watch_expiration),
                last_synced_at = now(),
                updated_at = now()
            WHERE id = %s
            """,
            (history_id, watch_expiration, mailbox_id),
        )
