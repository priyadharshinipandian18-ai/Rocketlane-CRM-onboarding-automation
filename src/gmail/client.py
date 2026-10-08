"""Thin wrapper around the Gmail API: register push notifications and read
the delta of new messages they announce.

Every method here is a plain blocking call (``googleapiclient`` has no
asyncio support) — callers must run them via ``asyncio.to_thread`` rather
than awaiting them directly.
"""
import base64
import logging
from email.message import EmailMessage as MimeMessage
from typing import NotRequired, TypedDict

from googleapiclient.errors import HttpError

from src.gmail.auth import get_gmail_service

log = logging.getLogger(__name__)


class EmailMessage(TypedDict):
    id: str
    sender: str
    subject: str
    received_at: str
    snippet: str
    labels: NotRequired[list[str]]
    """Gmail label IDs, e.g. ``INBOX`` / ``SENT``."""
    thread_id: NotRequired[str]
    """Gmail thread ID — how a reply is matched to the escalation email we sent."""


def _decode_part(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def _plain_text(payload: dict) -> str | None:
    """The first ``text/plain`` body in a (possibly multipart) Gmail payload."""
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return _decode_part(payload["body"]["data"])
    for part in payload.get("parts", []) or []:
        found = _plain_text(part)
        if found:
            return found
    return None


class GmailClient:
    def __init__(self, interactive: bool = False, send: bool = False):
        self._service = get_gmail_service(interactive=interactive, send=send)

    def send_message(self, to: str, subject: str, body: str, thread_id: str | None = None) -> dict:
        """Send a plain-text email from the authorised mailbox. Returns Gmail's
        ``{"id", "threadId", ...}`` — keep ``threadId`` to continue the thread."""
        mime = MimeMessage()
        mime["To"] = to
        mime["Subject"] = subject
        mime.set_content(body)
        request_body: dict = {"raw": base64.urlsafe_b64encode(mime.as_bytes()).decode()}
        if thread_id:
            request_body["threadId"] = thread_id
        sent = self._service.users().messages().send(userId="me", body=request_body).execute()
        log.info("Email sent | to=%s subject=%r id=%s", to, subject, sent.get("id"))
        return sent

    def get_message_text(self, message_id: str) -> str:
        """The plain-text body of one message (``get_message`` only has a ~200-char snippet)."""
        msg = self._service.users().messages().get(userId="me", id=message_id, format="full").execute()
        return _plain_text(msg.get("payload", {})) or msg.get("snippet", "")

    def register_watch(self, topic_name: str) -> dict:
        """Register (or renew) Gmail push notifications to a Pub/Sub topic.

        Gmail ``watch()`` registrations expire after 7 days — call this on a
        schedule (e.g. a daily cron) to keep notifications flowing. Returns
        the response containing the starting ``historyId``.
        """
        body = {"topicName": topic_name, "labelIds": ["INBOX"]}
        response = self._service.users().watch(userId="me", body=body).execute()
        log.info("Gmail watch registered | historyId=%s expiration=%s", response.get("historyId"), response.get("expiration"))
        return response

    def list_new_message_ids(self, start_history_id: str) -> tuple[list[str], str | None]:
        """Return (new_message_ids, latest_history_id) since ``start_history_id``.

        Returns ``(ids, None)`` if ``start_history_id`` is too old for Gmail
        to diff (history expires after ~7 days of inactivity) — the caller
        should fall back to a fresh ``watch()`` registration in that case.
        """
        try:
            message_ids: list[str] = []
            page_token = None
            latest_history_id = start_history_id
            while True:
                response = (
                    self._service.users()
                    .history()
                    .list(
                        userId="me",
                        startHistoryId=start_history_id,
                        historyTypes=["messageAdded"],
                        pageToken=page_token,
                    )
                    .execute()
                )
                for record in response.get("history", []):
                    for added in record.get("messagesAdded", []):
                        message_ids.append(added["message"]["id"])
                latest_history_id = response.get("historyId", latest_history_id)
                page_token = response.get("nextPageToken")
                if not page_token:
                    break
            return message_ids, latest_history_id
        except HttpError as exc:
            if exc.resp.status == 404:
                log.warning("Gmail historyId %s expired — needs a fresh watch()", start_history_id)
                return [], None
            raise

    def get_message(self, message_id: str) -> EmailMessage:
        """Fetch headers + snippet for one message (no full body parsing)."""
        msg = (
            self._service.users()
            .messages()
            .get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            )
            .execute()
        )
        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
        return EmailMessage(
            id=message_id,
            sender=headers.get("From", ""),
            subject=headers.get("Subject", ""),
            received_at=headers.get("Date", ""),
            snippet=msg.get("snippet", ""),
            labels=msg.get("labelIds", []),
            thread_id=msg.get("threadId", ""),
        )
