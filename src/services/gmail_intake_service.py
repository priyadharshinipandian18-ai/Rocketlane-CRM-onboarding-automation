"""Turns a Gmail push notification into workflow runs.

Gmail's ``watch()`` registration makes Gmail publish a small notification —
``{"emailAddress": "...", "historyId": "..."}`` — to the Pub/Sub topic on
every mailbox change. The notification carries no mail content; this service
is the "get the mail details" step: diff against ``app.mailboxes.history_id``
to find which messages are new, then for each one

1. read it and **redact PII** (only the redacted body is stored or checkpointed);
2. record it in ``app.inbound_emails`` — that row's uniqueness is what stops a replayed
   notification from starting a second onboarding;
3. run the **intake workflow** (classify: deal_closed / no_deal / escalation_reply / malicious);
4. a closed deal starts the tier workflow (the orchestrator); an *escalation reply* is handed to
   ``TierReplyService``, which resumes the paused tier run if the AE gave a clear answer.

One code path serves every transport: the Cloud Function entry point
(``functions/pubsub_intake.py``), the push endpoint (``POST /pubsub/push``)
and the pull subscriber (``PubSubPullService``).
"""
import asyncio
import base64
import json
import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable

from googleapiclient.errors import HttpError

from src.agents.email_analyser.guardrails import redact_pii
from src.gmail.client import GmailClient
from src.models.enums import EmailKind, InboundEmailStatus
from src.services import InboundEmailService, MailboxService
from src.services.tier_reply_service import TierReplyService
from src.services.workflow_orchestrator import WorkflowOrchestrator
from src.utils.errors import is_transient

log = logging.getLogger(__name__)


def decode_pubsub_data(data_b64: str) -> dict:
    return json.loads(base64.b64decode(data_b64).decode("utf-8"))


def parse_received_at(date_header: str) -> datetime:
    """Gmail's ``Date`` header is RFC 2822 (e.g. ``Tue, 6 Oct 2026 17:15:58
    +0000 (UTC)``), which Postgres can't parse; fall back to now if absent or
    malformed."""
    try:
        return parsedate_to_datetime(date_header)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


class GmailIntakeService:
    def __init__(
        self,
        mailboxes: MailboxService | None = None,
        inbound_emails: InboundEmailService | None = None,
        gmail_client_factory: Callable[[], Any] = GmailClient,
        orchestrator: WorkflowOrchestrator | None = None,
        tier_replies: TierReplyService | None = None,
    ) -> None:
        self.mailboxes = mailboxes or MailboxService()
        self.inbound_emails = inbound_emails or InboundEmailService()
        self._gmail_client_factory = gmail_client_factory
        self.orchestrator = orchestrator or WorkflowOrchestrator()
        self.tier_replies = tier_replies or TierReplyService(orchestrator=self.orchestrator)

    async def handle_pubsub_envelope(self, envelope: dict) -> None:
        """A raw Pub/Sub message envelope (push shape:
        ``{"message": {"data": "<base64>", ...}}``)."""
        await self.handle_gmail_notification(decode_pubsub_data(envelope["message"]["data"]))

    async def handle_gmail_notification(self, notification: dict) -> None:
        """Process one decoded Gmail-watch notification: record + classify each new message."""
        mailbox_email = notification["emailAddress"]
        current_history_id = int(notification["historyId"])
        mailbox = await self.mailboxes.get_or_create(mailbox_email)

        if mailbox.history_id is None:
            # First notification ever received for this mailbox — nothing to diff against yet.
            log.info("No prior history_id for mailbox %s; bootstrapping cursor to %s", mailbox_email, current_history_id)
            await self.mailboxes.update_cursor(mailbox.id, current_history_id)
            return

        client = self._gmail_client_factory()
        message_ids, latest_history_id = await asyncio.to_thread(client.list_new_message_ids, str(mailbox.history_id))

        if latest_history_id is None:
            # history_id too old to diff (Gmail history expired) — re-anchor on the
            # notification's own historyId and skip this cycle. A production system
            # should also re-register watch() here.
            log.warning("history_id expired for mailbox %s — re-anchoring to %s", mailbox_email, current_history_id)
            await self.mailboxes.update_cursor(mailbox.id, current_history_id)
            return

        log.info("Found %d new message(s) since history_id %s", len(message_ids), mailbox.history_id)
        for message_id in message_ids:
            await self._handle_message(client, mailbox.id, message_id)

        await self.mailboxes.update_cursor(mailbox.id, int(latest_history_id))

    async def _handle_message(self, client: Any, mailbox_id: str, message_id: str) -> None:
        """One message, isolated from the rest of the notification.

        *Transient* failures (network, Google 5xx/429, a missing Gmail permission) are re-raised:
        the whole notification is retried later, with a backoff, and the cursor stays put so
        nothing is lost. A *permanent* failure (a bug, odd data in this one message) would fail
        identically forever — it is logged once, recorded on the email row, and skipped, so it
        neither blocks the messages behind it nor makes Pub/Sub redeliver in a loop.
        """
        try:
            await self._process_message(client, mailbox_id, message_id)
        except Exception as exc:
            if is_transient(exc):
                raise
            log.exception("Skipping message %s — it failed in a way retrying will not fix", message_id)
            await self._record_failure(mailbox_id, message_id, exc)

    async def _record_failure(self, mailbox_id: str, message_id: str, exc: Exception) -> None:
        """Best effort: mark the stored email (if it got that far) as failed. Never raises."""
        try:
            stored = await self.inbound_emails.find_one(mailbox_id=mailbox_id, gmail_message_id=message_id)
            if stored is not None:
                await self.inbound_emails.update_classification(
                    stored.id, is_deal_email=False,
                    classification={"error": f"{type(exc).__name__}: {str(exc)[:300]}"},
                    status=InboundEmailStatus.FAILED,
                )
        except Exception:
            log.exception("Could not record the failure of message %s either", message_id)

    async def _process_message(self, client: Any, mailbox_id: str, message_id: str) -> None:
        try:
            email = await asyncio.to_thread(client.get_message, message_id)
        except HttpError as exc:
            if exc.resp.status == 404:
                # Listed in the history but gone by now (deleted / draft discarded). Skipping
                # it must not fail the whole notification, or the cursor never advances and
                # Pub/Sub redelivers it forever.
                log.info("Message %s no longer exists — skipping", message_id)
                return
            raise

        labels = email.get("labels") or []
        if "SENT" in labels and "INBOX" not in labels:
            # Our own outgoing mail (the escalation emails) shows up in the history too.
            log.debug("Message %s is outgoing — skipping", message_id)
            return

        body = await self._read_body(client, message_id, email)
        redacted, redactions = redact_pii(body)
        if redactions:
            log.info("Redacted PII from message %s | %s", message_id, redactions)

        inbound = await self.inbound_emails.try_insert(
            mailbox_id=mailbox_id,
            gmail_message_id=message_id,
            gmail_thread_id=email.get("thread_id") or None,
            from_address=email["sender"],
            subject=email["subject"],
            received_at=parse_received_at(email["received_at"]),
            body_redacted=redacted,
        )
        if inbound is None:
            log.info("Message %s already recorded — skipping (duplicate notification/replay)", message_id)
            return

        result = await self.orchestrator.process_inbound_email(
            inbound.id,
            {"sender": email["sender"], "subject": email["subject"], "body": redacted,
             "gmail_thread_id": email.get("thread_id") or None},
        )

        if result.get("kind") == EmailKind.ESCALATION_REPLY.value and result.get("escalation_id"):
            await self._deliver_reply(inbound.id, result["escalation_id"], email["sender"], redacted)

    @staticmethod
    async def _read_body(client: Any, message_id: str, email: dict) -> str:
        """The full plain-text body; the ~200-character snippet if it can't be read."""
        try:
            return await asyncio.to_thread(client.get_message_text, message_id)
        except Exception:
            log.exception("Could not read the full body of %s — using the snippet", message_id)
            return email.get("snippet", "")

    async def _deliver_reply(self, inbound_email_id: str, escalation_id: str, sender: str, text: str) -> None:
        try:
            outcome = await self.tier_replies.deliver_reply(escalation_id, inbound_email_id, sender, text)
        except Exception:
            log.exception("Handling the escalation reply %s failed", inbound_email_id)
            outcome = "error"
        log.info("Escalation reply %s → %s", inbound_email_id, outcome)
        await self.inbound_emails.update_classification(
            inbound_email_id, is_deal_email=False, classification={"escalation_reply": True, "outcome": outcome},
            status=InboundEmailStatus.PARSED,
        )
