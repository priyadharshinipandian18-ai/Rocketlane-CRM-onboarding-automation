"""``GmailIntakeService``: notification -> read + redact -> record -> classify -> hand off.

Every collaborator is injected, so each is replaced by a small in-memory fake — no Gmail,
Postgres or graphs. (What the classifier decides is covered in test_email_analyser.py; the
full flows against a real database are in test_human_tier_flow.py / test_voice_flow_integration.py.)
"""
import base64
import json
import os

os.environ.setdefault("GOOGLE_API_KEY", "test-key-not-used")

from types import SimpleNamespace

import pytest
from googleapiclient.errors import HttpError

from src.models.entities import InboundEmail, Mailbox
from src.services.gmail_intake_service import GmailIntakeService

NOTIFICATION = {"emailAddress": "cs@rappit.io", "historyId": "200"}
DEAL = {
    "id": "m1", "sender": "Alex AE <alex@rappit.io>", "subject": "Closed!", "received_at": "Tue, 6 Oct 2026 17:15:58 +0000 (UTC)",
    "snippet": "We just closed", "labels": ["INBOX"], "thread_id": "T-deal",
}
BODY = "We closed Acme! Call me on +1 415-555-0132 or mail jane@acme.com"


class FakeGmail:
    def __init__(self, message, body=BODY, message_ids=("m1",), body_error=False):
        self.message, self.body, self.message_ids, self.body_error = message, body, list(message_ids), body_error

    def list_new_message_ids(self, _history_id):
        return self.message_ids, "200"

    def get_message(self, _id):
        if isinstance(self.message, Exception):
            raise self.message
        return self.message

    def get_message_text(self, _id):
        if self.body_error:
            raise RuntimeError("cannot read the full body")
        return self.body


@pytest.fixture
def build():
    def make(message, *, body=BODY, duplicate=False, kind=None, escalation_id=None, body_error=False, message_ids=("m1",)):
        record = SimpleNamespace(inserted=[], processed=[], delivered=[], classified=[], cursor=[])

        class Mailboxes:
            async def get_or_create(self, _email):
                return Mailbox(id="mb1", email="cs@rappit.io", history_id=100)

            async def update_cursor(self, mailbox_id, history_id, watch_expiration=None):
                record.cursor.append(history_id)

        class InboundEmails:
            async def try_insert(self, **kwargs):
                record.inserted.append(kwargs)
                return None if duplicate else InboundEmail(id="e1", mailbox_id="mb1", gmail_message_id="m1")

            async def update_classification(self, email_id, is_deal_email, classification, status, **_):
                record.classified.append((email_id, classification, status))

        class Orchestrator:
            async def process_inbound_email(self, inbound_id, email):
                record.processed.append((inbound_id, email))
                return {"kind": kind, "escalation_id": escalation_id} if kind else {}

        class TierReplies:
            async def deliver_reply(self, escalation_id, inbound_id, sender, text):
                record.delivered.append((escalation_id, inbound_id, sender, text))
                return "resumed"

        service = GmailIntakeService(
            mailboxes=Mailboxes(), inbound_emails=InboundEmails(),
            gmail_client_factory=lambda: FakeGmail(message, body, message_ids, body_error),
            orchestrator=Orchestrator(), tier_replies=TierReplies(),
        )
        return service, record

    return make


async def test_a_new_email_is_redacted_recorded_and_handed_to_the_intake_workflow(build):
    service, record = build(DEAL, kind="deal_closed")
    await service.handle_gmail_notification(NOTIFICATION)

    stored = record.inserted[0]
    assert stored["gmail_thread_id"] == "T-deal"
    assert "[REDACTED_PHONE]" in stored["body_redacted"] and "+1 415-555-0132" not in stored["body_redacted"]
    assert "jane@acme.com" in stored["body_redacted"]                       # contact emails are kept for extraction
    assert stored["received_at"].year == 2026                                # the RFC 2822 Date header was parsed

    inbound_id, email = record.processed[0]
    assert inbound_id == "e1" and email["body"] == stored["body_redacted"]   # the raw body never reaches the graph
    assert email["gmail_thread_id"] == "T-deal" and email["sender"] == DEAL["sender"]
    assert record.delivered == [] and record.cursor == [200]


async def test_an_escalation_reply_is_handed_to_the_reply_service_with_the_redacted_text(build):
    service, record = build(DEAL, body="Enterprise", kind="escalation_reply", escalation_id="esc-1")
    await service.handle_gmail_notification(NOTIFICATION)
    assert record.delivered == [("esc-1", "e1", DEAL["sender"], "Enterprise")]
    assert record.classified[-1][1] == {"escalation_reply": True, "outcome": "resumed"}


async def test_other_kinds_are_not_delivered_anywhere(build):
    for kind in ("no_deal", "malicious", "deal_closed"):
        service, record = build(DEAL, kind=kind)
        await service.handle_gmail_notification(NOTIFICATION)
        assert record.delivered == []


async def test_our_own_outgoing_mail_is_skipped(build):
    service, record = build({**DEAL, "labels": ["SENT"]})
    await service.handle_gmail_notification(NOTIFICATION)
    assert record.inserted == [] and record.processed == [] and record.cursor == [200]


async def test_a_sent_copy_that_is_also_in_the_inbox_is_still_processed(build):
    service, record = build({**DEAL, "labels": ["SENT", "INBOX"]})
    await service.handle_gmail_notification(NOTIFICATION)
    assert len(record.processed) == 1


async def test_a_replayed_message_is_not_processed_twice(build):
    service, record = build(DEAL, duplicate=True)
    await service.handle_gmail_notification(NOTIFICATION)
    assert record.processed == [] and record.cursor == [200]


async def test_a_message_deleted_before_we_read_it_does_not_block_the_cursor(build):
    gone = HttpError(SimpleNamespace(status=404, reason="Not Found"), b"{}")
    service, record = build(gone)
    await service.handle_gmail_notification(NOTIFICATION)
    assert record.processed == [] and record.cursor == [200]


async def test_the_snippet_is_used_when_the_full_body_cannot_be_read(build):
    service, record = build(DEAL, body_error=True)
    await service.handle_gmail_notification(NOTIFICATION)
    assert record.inserted[0]["body_redacted"] == "We just closed"


async def test_pubsub_envelope_is_decoded(build):
    service, record = build(DEAL)
    data = base64.b64encode(json.dumps(NOTIFICATION).encode()).decode()
    await service.handle_pubsub_envelope({"message": {"data": data}})
    assert len(record.processed) == 1
