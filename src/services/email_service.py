import asyncio
import logging
from typing import Any, Callable

from src.gmail.client import GmailClient
from src.models.base_model import BaseModel

log = logging.getLogger(__name__)


class SentEmail(BaseModel):
    message_id: str | None = None
    thread_id: str | None = None


class EmailService:
    """Sends mail from the authorised Gmail mailbox (``gmail.send`` scope).

    Replies come back to the same mailbox, which the Gmail watch already
    monitors — that is how an AE's answer reaches ``TierReplyService``.
    """

    def __init__(self, gmail_client_factory: Callable[[], Any] | None = None) -> None:
        # Sending is the only thing that needs the gmail.send permission.
        self._gmail_client_factory = gmail_client_factory or (lambda: GmailClient(send=True))

    async def send(self, to: str, subject: str, body: str, thread_id: str | None = None) -> SentEmail:
        # GmailClient is blocking (googleapiclient has no asyncio support), so build and call it off-loop.
        sent = await asyncio.to_thread(lambda: self._gmail_client_factory().send_message(to, subject, body, thread_id))
        return SentEmail(message_id=sent.get("id"), thread_id=sent.get("threadId"))
