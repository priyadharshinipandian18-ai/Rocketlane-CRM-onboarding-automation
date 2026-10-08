"""Registers (or renews) Gmail push notifications to the Pub/Sub topic and
records the starting ``history_id`` + expiration in ``app.mailboxes``.

Gmail ``watch()`` registrations expire after 7 days, so this runs once to
start and then on a schedule (e.g. Cloud Scheduler → ``POST /gmail/watch``)
to keep notifications flowing.
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from src.api.exceptions import AppException, ErrorCode
from src.config.settings import get_settings
from src.gmail.client import GmailClient
from src.models.dto.response import GmailWatchResponse
from src.services import MailboxService

log = logging.getLogger(__name__)


class GmailWatchService:
    def __init__(
        self,
        mailboxes: MailboxService | None = None,
        gmail_client_factory: Callable[[], Any] = GmailClient,
    ) -> None:
        self.mailboxes = mailboxes or MailboxService()
        self._gmail_client_factory = gmail_client_factory

    async def register(self) -> GmailWatchResponse:
        settings = get_settings()
        if not settings.gmail_watch_topic or not settings.gmail_mailbox_email:
            raise AppException(ErrorCode.CONFIG_MISSING, "Set GMAIL_WATCH_TOPIC and GMAIL_MAILBOX_EMAIL in .env first.")

        # GmailClient is blocking (googleapiclient has no asyncio support), so build and call it off-loop.
        response = await asyncio.to_thread(lambda: self._gmail_client_factory().register_watch(settings.gmail_watch_topic))

        watch_expiration = datetime.fromtimestamp(int(response["expiration"]) / 1000, tz=timezone.utc)
        history_id = int(response["historyId"])
        mailbox = await self.mailboxes.get_or_create(settings.gmail_mailbox_email)
        await self.mailboxes.update_cursor(mailbox.id, history_id, watch_expiration)

        log.info("Gmail watch registered for %s until %s", settings.gmail_mailbox_email, watch_expiration.isoformat())
        return GmailWatchResponse(
            mailbox_email=settings.gmail_mailbox_email,
            topic=settings.gmail_watch_topic,
            history_id=history_id,
            watch_expiration=watch_expiration,
        )
