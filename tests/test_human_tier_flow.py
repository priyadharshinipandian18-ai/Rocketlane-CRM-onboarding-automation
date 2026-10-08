"""The "AE didn't answer the call" path, end to end against a REAL Postgres (app tables + the
LangGraph checkpointer). Only the outside world is faked: Gemini, Rocketlane and Gmail.

    deal email → intake → tier workflow: no phone → ESCALATION EMAIL to the AE → run parks
    (waiting / email_reply) → the AE's reply is classified, authorised, parsed → the tier run
    resumes → provisioning starts at create_rocketlane_project.

Also: timeout → stopped → late reply / re-open, an unclear or foreign reply, an email that
fails to send, and a provisioning failure retried from the node that failed.
"""
import os
import uuid
from datetime import datetime, timezone
from itertools import count

os.environ.setdefault("GOOGLE_API_KEY", "test-key-not-used")

import httpx
import psycopg
import pytest
from psycopg.rows import dict_row

from src.agents.email_analyser import nodes as analyser_nodes
from src.agents.email_analyser.models import DealStatusAnalysis
from src.agents.intake import nodes as intake_nodes
from src.agents.intake.models import DealEmailExtraction
from src.agents.provisioning import nodes as provisioning_nodes
from src.services import human_tier_notifier
from src.services.gmail_intake_service import GmailIntakeService
from src.services.tier_escalation_service import TierEscalationService
from src.services.workflow_orchestrator import WorkflowOrchestrator
from tests.conftest import reset_app_singletons
from tests.fakes import FakeEmailService

ADMIN = {"x-admin-token": "test-admin-token"}
AE = "raj@novacrm.test"
CLOSED_BODY = "Great news! Acme Corp signed the contract today. Closed-won! Sending the signed order form attached."


class FakeRocketlane:
    created: list[dict] = []
    fail = False

    import_fail = False
    create_calls = 0

    async def create_project(self, **kwargs):
        if FakeRocketlane.fail:
            raise RuntimeError("Rocketlane is down")
        FakeRocketlane.create_calls += 1
        FakeRocketlane.created.append(kwargs)
        return {"projectId": 4242, "projectUrl": "https://rocketlane.example/projects/4242"}

    async def import_template(self, project_id, template_id, start_date):
        if FakeRocketlane.import_fail:
            raise RuntimeError("template import refused")
        FakeRocketlane.created[-1]["template_id"] = template_id


class FakeGmail:
    """One reply message, as Gmail would report it."""

    def __init__(self, message_id, sender, subject, thread_id, text):
        self.message_id, self.text = message_id, text
        self.message = {
            "id": message_id, "sender": sender, "subject": subject, "received_at": "Wed, 7 Oct 2026 10:00:00 +0000",
            "snippet": text[:100], "labels": ["INBOX"], "thread_id": thread_id,
        }

    def list_new_message_ids(self, _history_id):
        return [self.message_id], "300"

    def get_message(self, _id):
        return self.message

    def get_message_text(self, _id):
        return self.text


@pytest.fixture
async def flow(temp_database, monkeypatch):
    with psycopg.connect(temp_database, autocommit=True) as conn:
        conn.execute("""
            INSERT INTO app.plan_tiers (code, display_name, rocketlane_template_id, duration_days, csm_model,
                                        slack_channel_prefix, welcome_message_template) VALUES
              ('enterprise','Enterprise','111',30,'dedicated','nova-ent-','Welcome {customer_name}'),
              ('growth','Growth','222',14,'pooled','nova-','Welcome {customer_name}');
            INSERT INTO app.cs_team_members (name, email, role, pool, rocketlane_user_id) VALUES
              ('Asha','asha@novacrm.test','csm','enterprise','rl-asha'),
              ('Ben','ben@novacrm.test','csm','growth','rl-ben'),
              ('Priya','priya@novacrm.test','cs_manager',NULL,'rl-priya');
            INSERT INTO app.mailboxes (email, history_id) VALUES ('cs@novacrm.test', 100);
        """)
    for name, value in (("SLACK_BOT_TOKEN", ""), ("REQUIRE_AE_SENDER", "false"), ("ADMIN_API_TOKEN", "test-admin-token")):
        monkeypatch.setenv(name, value)
    reset_app_singletons()

    FakeRocketlane.created, FakeRocketlane.fail = [], False
    FakeRocketlane.import_fail, FakeRocketlane.create_calls = False, 0
    FakeEmailService.reset()
    monkeypatch.setattr(provisioning_nodes, "RocketlaneClient", FakeRocketlane)
    monkeypatch.setattr(human_tier_notifier, "EmailService", FakeEmailService)       # never email for real

    class ClosedDeal:
        async def ainvoke(self, _payload):
            return DealStatusAnalysis(
                is_deal_closed=True, confidence_score=0.95,
                closing_evidence=["Acme Corp signed the contract today"], reasoning="Signed contract.",
            )

    class Extraction:
        async def ainvoke(self, _payload):
            return DealEmailExtraction(
                is_deal_closed=True, confidence="high", customer_name="Acme Corp", customer_contact_name="Jane Doe",
                customer_contact_email="jane@acme.test", ae_name="Raj Kumar", ae_email=AE,
                sf_opportunity_id=f"006{uuid.uuid4().hex[:8]}", deal_summary="Acme signed.", reasoning="Closed-won.",
            )

    monkeypatch.setattr(analyser_nodes, "_get_analyser_chain", lambda: ClosedDeal())
    monkeypatch.setattr(intake_nodes, "_get_deal_parser_chain", lambda: Extraction())

    def db(sql, params=()):
        with psycopg.connect(temp_database, autocommit=True, row_factory=dict_row) as c:
            return c.execute(sql, params).fetchall()

    async def deal() -> str:
        """A closed-deal email from an AE with no phone on file: the call can't be placed."""
        mailbox_id = db("SELECT id FROM app.mailboxes")[0]["id"]
        inbound_id = db(
            "INSERT INTO app.inbound_emails (mailbox_id, gmail_message_id, from_address, subject) "
            "VALUES (%s, %s, %s, %s) RETURNING id", (mailbox_id, uuid.uuid4().hex, AE, "Closed: Acme Corp"),
        )[0]["id"]
        await WorkflowOrchestrator().process_inbound_email(
            str(inbound_id), {"sender": AE, "subject": "Closed: Acme Corp", "body": CLOSED_BODY, "gmail_thread_id": "T-deal"}
        )
        return str(db("SELECT id FROM app.onboardings WHERE inbound_email_id = %s", (inbound_id,))[0]["id"])

    ids = count(1)

    async def reply(text, *, sender=f"Raj Kumar <{AE}>", thread="thread-1", subject="Re: Plan tier needed") -> str:
        """The AE (or someone) replies; Gmail's watch fires; the app handles the notification."""
        message_id = f"reply-{next(ids)}"
        service = GmailIntakeService(gmail_client_factory=lambda: FakeGmail(message_id, sender, subject, thread, text))
        await service.handle_gmail_notification({"emailAddress": "cs@novacrm.test", "historyId": "300"})
        return message_id

    from main import app

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield type("Flow", (), {"db": staticmethod(db), "deal": staticmethod(deal), "reply": staticmethod(reply), "client": client})


def run(flow, oid, workflow="tier_confirmation"):
    return flow.db("SELECT * FROM app.workflow_runs WHERE onboarding_id = %s AND workflow = %s", (oid, workflow))[0]


def onboarding(flow, oid):
    return flow.db("SELECT * FROM app.onboardings WHERE id = %s", (oid,))[0]


def escalations(flow, oid):
    return flow.db("SELECT * FROM app.escalations WHERE onboarding_id = %s ORDER BY attempt_no", (oid,))


def audit(flow, oid):
    return [r["event_type"] for r in flow.db("SELECT event_type FROM app.audit_events WHERE onboarding_id = %s ORDER BY id", (oid,))]


def assert_provisioned(flow, oid, template):
    assert onboarding(flow, oid)["status"] == "project_created"
    assert [c["template_id"] for c in FakeRocketlane.created] == [template]
    assert run(flow, oid, "provisioning")["status"] == "succeeded"


# ── the escalation ──────────────────────────────────────────────────────────

async def test_when_the_call_cannot_be_placed_the_ae_is_emailed_and_the_run_parks(flow):
    oid = await flow.deal()

    token = f"ONB-{oid.replace('-', '')[:8]}"
    [mail] = FakeEmailService.to(AE)
    assert mail["subject"] == f"[{token}] Plan tier needed: Acme Corp"
    assert "ENTERPRISE" in mail["body"] and "GROWTH" in mail["body"]

    [esc] = escalations(flow, oid)
    assert (esc["status"], esc["attempt_no"], esc["to_email"], esc["token"], esc["gmail_thread_id"]) == (
        "sent", 1, AE, token, "thread-1")

    tier = run(flow, oid)
    assert (tier["status"], tier["waiting_for"], tier["current_node"]) == ("waiting", "email_reply", "await_ae_reply")
    assert tier["thread_id"] == f"tier:{oid}"
    assert 23 < (tier["stop_at"] - datetime.now(timezone.utc)).total_seconds() / 3600 <= 24     # HUMAN_STOP_AFTER_HOURS
    assert onboarding(flow, oid)["status"] == "needs_human_review"
    assert flow.db("SELECT 1 FROM app.workflow_runs WHERE workflow = 'provisioning'") == []     # nothing provisioned yet
    assert FakeRocketlane.created == []

    intake = flow.db("SELECT * FROM app.workflow_runs WHERE workflow = 'intake'")[0]
    assert (intake["status"], intake["outcome"], str(intake["onboarding_id"])) == ("succeeded", "deal_closed", oid)


async def test_the_aes_reply_resumes_the_flow_from_create_rocketlane_project(flow):
    oid = await flow.deal()
    reply_id = await flow.reply("Enterprise\n\nOn Wed, Oct 7, 2026 at 10:00 AM Bot <b@x.co> wrote:\n> reply ENTERPRISE or GROWTH")

    assert_provisioned(flow, oid, "111")
    assert FakeRocketlane.created[0]["owner_email"] == "priya@novacrm.test"            # the project owner is the CS manager
    assert FakeRocketlane.created[0]["customer_contact_email"] == "asha@novacrm.test"  # the Enterprise CSM's email
    ob = onboarding(flow, oid)
    assert (ob["plan_tier"], ob["plan_tier_source"]) == ("enterprise", "manual_override")

    tier, provisioning = run(flow, oid), run(flow, oid, "provisioning")
    assert (tier["status"], tier["outcome"], tier["waiting_for"]) == ("succeeded", "tier:enterprise", None)
    assert provisioning["thread_id"] == f"provision:{oid}" and provisioning["outcome"].startswith("project")

    [esc] = escalations(flow, oid)
    inbound = flow.db("SELECT * FROM app.inbound_emails WHERE gmail_message_id = %s", (reply_id,))[0]
    assert (esc["status"], esc["answered_tier"], esc["answered_by"]) == ("answered", "enterprise", AE)
    assert esc["answered_inbound_email_id"] == inbound["id"]
    assert (inbound["kind"], inbound["kind_source"], inbound["escalation_id"]) == ("escalation_reply", "thread_match", esc["id"])
    assert inbound["classification"]["outcome"] == "resumed" and inbound["status"] == "parsed"
    assert "Enterprise" in inbound["body_redacted"]

    events = audit(flow, oid)
    for expected in ("human_tier_requested", "tier_reply_accepted", "tier_confirmed", "tier_workflow_completed", "project_created"):
        assert expected in events


async def test_a_reply_is_matched_by_the_subject_token_when_the_thread_differs(flow):
    oid = await flow.deal()
    token = f"ONB-{oid.replace('-', '')[:8]}"
    await flow.reply("growth", thread="a-different-thread", subject=f"Re: [{token}] Plan tier needed: Acme Corp")

    assert_provisioned(flow, oid, "222")                                               # Growth template
    assert flow.db("SELECT kind_source FROM app.inbound_emails WHERE kind = 'escalation_reply'")[0]["kind_source"] == "token"


async def test_an_unclear_reply_asks_again_but_never_picks_a_tier_and_stops_nagging(flow):
    oid = await flow.deal()
    for text in ("Enterprise or Growth?", "not sure yet", "hmm let me check"):
        await flow.reply(text)

    assert run(flow, oid)["waiting_for"] == "email_reply" and FakeRocketlane.created == []
    assert onboarding(flow, oid)["plan_tier"] is None
    nudges = [m for m in FakeEmailService.to(AE) if "one word" in m["subject"].lower()]
    assert len(nudges) == 2                                                            # MAX_CLARIFICATIONS: an auto-responder can't loop us

    await flow.reply("Growth")                                                         # a clear answer still works afterwards
    assert_provisioned(flow, oid, "222")


async def test_only_the_ae_we_emailed_may_answer(flow):
    oid = await flow.deal()
    await flow.reply("Enterprise", sender="Mallory <mallory@evil.test>")

    assert run(flow, oid)["waiting_for"] == "email_reply" and FakeRocketlane.created == []
    assert "tier_reply_rejected" in audit(flow, oid)
    assert escalations(flow, oid)[0]["status"] == "sent"


# ── giving up, and continuing later ─────────────────────────────────────────

def _expire(flow, oid):
    flow.db("UPDATE app.workflow_runs SET stop_at = now() - interval '1 minute' WHERE onboarding_id = %s AND workflow = 'tier_confirmation'"
            " RETURNING id", (oid,))


async def test_no_reply_in_time_stops_the_run_and_a_late_reply_still_continues_it(flow):
    oid = await flow.deal()
    assert await TierEscalationService().run_due_once() == 0                           # not due yet

    _expire(flow, oid)
    assert await TierEscalationService().run_due_once() == 1
    tier = run(flow, oid)
    assert (tier["status"], tier["waiting_for"], tier["current_node"]) == ("stopped", None, "await_ae_reply")
    assert "stopped" in onboarding(flow, oid)["status_reason"] and "human_wait_stopped" in audit(flow, oid)
    assert await TierEscalationService().run_due_once() == 0                           # only once

    await flow.reply("Enterprise")                                                     # the AE finally answers
    assert_provisioned(flow, oid, "111")
    assert run(flow, oid)["status"] == "succeeded"


async def test_resume_reopens_a_stopped_run_and_asks_the_ae_again(flow):
    oid = await flow.deal()
    _expire(flow, oid)
    await TierEscalationService().run_due_once()

    assert (await flow.client.post(f"/onboardings/{oid}/resume", headers=ADMIN)).status_code == 200
    reopened = run(flow, oid)
    assert (reopened["status"], reopened["waiting_for"], reopened["current_node"]) == ("waiting", "email_reply", "await_ae_reply")
    assert reopened["stop_at"] > datetime.now(timezone.utc)

    first, second = escalations(flow, oid)
    assert (first["status"], second["status"], second["attempt_no"]) == ("superseded", "sent", 2)
    assert len(FakeEmailService.to(AE)) == 2
    assert (await flow.client.post(f"/onboardings/{oid}/resume", headers=ADMIN)).status_code == 409   # no longer stopped

    await flow.reply("Growth")
    assert_provisioned(flow, oid, "222")


async def test_an_operator_can_resume_a_stopped_run_with_the_tier(flow):
    oid = await flow.deal()
    _expire(flow, oid)
    await TierEscalationService().run_due_once()

    body = {"plan_tier": "enterprise", "reviewer": "priya@novacrm.test"}
    assert (await flow.client.post(f"/onboardings/{oid}/resume", json=body, headers=ADMIN)).json()["status"] == "resuming"
    assert_provisioned(flow, oid, "111")
    assert escalations(flow, oid)[0]["answered_by"] == "priya@novacrm.test"


async def test_a_failed_email_send_does_not_break_the_wait(flow):
    FakeEmailService.fail = True
    oid = await flow.deal()

    assert escalations(flow, oid)[0]["status"] == "failed" and "human_notify_failed" in audit(flow, oid)
    assert run(flow, oid)["waiting_for"] == "email_reply"                              # still parked; an operator can answer
    body = {"plan_tier": "growth", "reviewer": "priya@novacrm.test"}
    assert (await flow.client.post(f"/onboardings/{oid}/plan-tier", json=body, headers=ADMIN)).status_code == 200
    assert_provisioned(flow, oid, "222")


# ── provisioning: failure, and a manual retry from the failing node ─────────

async def test_a_provisioning_failure_stops_at_the_failing_node_and_retry_continues_from_there(flow):
    oid = await flow.deal()
    FakeRocketlane.fail = True
    await flow.reply("Enterprise")

    provisioning = run(flow, oid, "provisioning")
    assert (provisioning["status"], provisioning["current_node"]) == ("failed", "create_rocketlane_project")
    assert "Rocketlane is down" in provisioning["last_error"]
    assert run(flow, oid)["status"] == "succeeded"                                     # the tier part is done and stays done
    assert onboarding(flow, oid)["status"] == "failed" and FakeRocketlane.created == []

    runs = (await flow.client.get(f"/onboardings/{oid}/runs", headers=ADMIN)).json()
    assert {r["workflow"]: r["status"] for r in runs} == {
        "intake": "succeeded", "tier_confirmation": "succeeded", "provisioning": "failed"}      # one row per part

    FakeRocketlane.fail = False
    assert (await flow.client.post(f"/onboardings/{oid}/retry", headers=ADMIN)).status_code == 200
    assert_provisioned(flow, oid, "111")                                               # exactly one project, no duplicate
    assert run(flow, oid, "provisioning")["attempt_count"] == 1
    assert (await flow.client.post(f"/onboardings/{oid}/retry", headers=ADMIN)).status_code == 409   # nothing failed any more


async def test_a_failed_template_import_is_skipped_and_provisioning_carries_on(flow):
    oid = await flow.deal()
    FakeRocketlane.import_fail = True
    await flow.reply("Enterprise")

    provisioning = run(flow, oid, "provisioning")
    assert provisioning["status"] == "succeeded"                                       # the run is not stopped by the import
    assert FakeRocketlane.create_calls == 1 and "template_id" not in FakeRocketlane.created[0]   # created, template not applied
    assert onboarding(flow, oid)["status"] == "project_created"
    assert "template_import_skipped" in audit(flow, oid)


async def test_a_lost_handoff_is_picked_up_by_the_sweeper(flow):
    """Tier finished and queued provisioning as ``pending`` — then the app died before starting it."""
    oid = await flow.deal()
    flow.db("UPDATE app.onboardings SET plan_tier = 'enterprise' WHERE id = %s RETURNING id", (oid,))
    flow.db("INSERT INTO app.workflow_runs (workflow, thread_id, onboarding_id) VALUES ('provisioning', %s, %s) RETURNING id",
            (f"provision:{oid}", oid))

    assert await WorkflowOrchestrator().start_pending() == 1
    assert_provisioned(flow, oid, "111")
    assert await WorkflowOrchestrator().start_pending() == 0
