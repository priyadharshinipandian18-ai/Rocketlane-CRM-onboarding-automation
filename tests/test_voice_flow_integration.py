"""End-to-end test of the AE plan-tier call flow against a REAL Postgres
(app tables + LangGraph checkpointer), with only the external services faked:
Gemini (email analysis/parsing), Bolna (placing calls + execution lookup)
and Rocketlane. Slack is disabled, so the communication agent is skipped.

Results arrive either by polling (default mode — the poller asks the faked
Bolna API) or through the real FastAPI webhook, shaped like Bolna's
execution webhooks. Covers: intermediate statuses, retry after no answer,
read-back + confidence requirements, repeated/unverified webhooks, Bolna
API down, retries exhausted → manual tier, out-of-hours deferral, and an AE
with no phone on file.

Needs a reachable Postgres server. It creates (and drops) its own temporary
database, using TEST_POSTGRES_ADMIN_URI or, failing that, the server from
POSTGRES_URI. Skips cleanly if none is reachable.
"""
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

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
from src.agents.tier_confirmation import nodes as tier_nodes
from src.api.routes import webhook_router
from src.services import call_result_service, human_tier_notifier
from tests.conftest import reset_app_singletons
from tests.fakes import FakeEmailService

ROOT = Path(__file__).resolve().parents[1]
WEBHOOK_TOKEN = "test-webhook-token"
ADMIN_TOKEN = "test-admin-token"
MONDAY_10_UTC = datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc)
CLOSED_BODY = "Great news! Acme Corp signed the contract today. Closed-won! Sending the signed order form attached."


def _server_base() -> str:
    uri = os.environ.get("TEST_POSTGRES_ADMIN_URI") or os.environ.get(
        "POSTGRES_URI", "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
    )
    return uri.rsplit("/", 1)[0]


_reset_singletons = reset_app_singletons


class FakeBolna:
    """Stands in for both halves of the Bolna API: placing calls and the
    authoritative execution lookup the webhook does before trusting a result."""
    calls: list[dict] = []
    executions: dict[str, dict] = {}
    api_down = False

    async def send_call(self, phone_number, ae_name, customer_name, metadata=None):
        FakeBolna.calls.append({"phone": phone_number, "ae": ae_name, "customer": customer_name, "metadata": metadata})
        execution_id = f"exec-{len(FakeBolna.calls)}"
        FakeBolna.executions[execution_id] = {"id": execution_id, "status": "queued"}
        return execution_id

    async def get_execution(self, execution_id):
        if FakeBolna.api_down:
            raise RuntimeError("Bolna API unavailable")
        return FakeBolna.executions[execution_id]


class FakeRocketlane:
    created: list[dict] = []

    async def create_project(self, **kwargs):
        FakeRocketlane.created.append(kwargs)
        return {"projectId": 4242, "projectUrl": "https://rocketlane.example/projects/4242"}

    async def import_template(self, project_id, template_id, start_date):
        FakeRocketlane.created[-1]["template_id"] = template_id              # the template lands on the project just created


def _extraction(ae_email: str) -> DealEmailExtraction:
    return DealEmailExtraction(
        is_deal_closed=True, confidence="high",
        customer_name="Acme Corp", customer_contact_name="Jane Doe", customer_contact_email="jane@acme.test",
        ae_name="Raj Kumar", ae_email=ae_email,
        sf_opportunity_id=f"006{uuid.uuid4().hex[:8]}", deal_summary="Acme signed.", reasoning="Closed-won email.",
    )


@pytest.fixture
async def env(monkeypatch):
    base = _server_base()
    try:
        admin = psycopg.connect(f"{base}/postgres", autocommit=True, connect_timeout=3)
    except Exception as exc:
        pytest.skip(f"No reachable Postgres server ({exc!r}) — set TEST_POSTGRES_ADMIN_URI or POSTGRES_URI")
    db_name = f"test_onboarding_{uuid.uuid4().hex[:10]}"
    admin.execute(f'CREATE DATABASE "{db_name}"')
    db_uri = f"{base}/{db_name}"

    with psycopg.connect(db_uri, autocommit=True) as conn:
        conn.execute((ROOT / "db" / "schema.sql").read_text(encoding="utf-8"))
        conn.execute("""
            INSERT INTO app.plan_tiers (code, display_name, rocketlane_template_id, duration_days, csm_model,
                                        slack_channel_prefix, welcome_message_template) VALUES
              ('enterprise','Enterprise','111',30,'dedicated','nova-ent-','Welcome {customer_name}'),
              ('growth','Growth','222',14,'pooled','nova-','Welcome {customer_name}');
            INSERT INTO app.account_executives (name, email, phone_e164, timezone)
              VALUES ('Raj Kumar','raj@novacrm.test','+14155550123','UTC');
            INSERT INTO app.cs_team_members (name, email, role, pool, rocketlane_user_id) VALUES
              ('Asha','asha@novacrm.test','csm','enterprise','rl-asha'),
              ('Ben','ben@novacrm.test','csm','growth','rl-ben'),
              ('Priya','priya@novacrm.test','cs_manager',NULL,'rl-priya');
            INSERT INTO app.mailboxes (email) VALUES ('cs@novacrm.test');
        """)

    monkeypatch.setenv("POSTGRES_URI", db_uri)
    monkeypatch.setenv("BOLNA_WEBHOOK_TOKEN", WEBHOOK_TOKEN)
    monkeypatch.setenv("BOLNA_ALLOWED_IPS", "")
    monkeypatch.setenv("BOLNA_MIN_CONFIDENCE", "0.7")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "")   # communication agent skipped
    monkeypatch.setenv("REQUIRE_AE_SENDER", "false")   # the sender gate is covered in test_email_analyser.py
    monkeypatch.setenv("ADMIN_API_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("VOICE_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("VOICE_RETRY_DELAY_MINUTES", "30")
    _reset_singletons()

    FakeBolna.calls, FakeBolna.executions, FakeBolna.api_down = [], {}, False
    FakeRocketlane.created = []
    monkeypatch.setattr(tier_nodes, "BolnaVoiceClient", FakeBolna)
    monkeypatch.setattr(webhook_router, "BolnaVoiceClient", FakeBolna)
    monkeypatch.setattr(call_result_service, "BolnaVoiceClient", FakeBolna)

    class FakeAnalyser:
        async def ainvoke(self, payload):
            return DealStatusAnalysis(
                is_deal_closed=True, confidence_score=0.95,
                closing_evidence=["Acme Corp signed the contract today"], reasoning="Signed contract.",
            )

    monkeypatch.setattr(analyser_nodes, "_get_analyser_chain", lambda: FakeAnalyser())
    monkeypatch.setattr(provisioning_nodes, "RocketlaneClient", FakeRocketlane)
    FakeEmailService.reset()
    monkeypatch.setattr(human_tier_notifier, "EmailService", FakeEmailService)   # never email for real
    monkeypatch.setattr(tier_nodes, "_now", lambda: MONDAY_10_UTC)

    def db(sql, params=()):
        with psycopg.connect(db_uri, autocommit=True, row_factory=dict_row) as c:
            return c.execute(sql, params).fetchall()

    async def start_deal(ae_email="raj@novacrm.test"):
        extraction = _extraction(ae_email)

        class FakeChain:
            async def ainvoke(self, payload):
                return extraction

        monkeypatch.setattr(intake_nodes, "_get_deal_parser_chain", lambda: FakeChain())
        mailbox_id = db("SELECT id FROM app.mailboxes")[0]["id"]
        inbound_id = db(
            "INSERT INTO app.inbound_emails (mailbox_id, gmail_message_id, from_address, subject) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (mailbox_id, uuid.uuid4().hex, ae_email, "Closed: Acme Corp"),
        )[0]["id"]
        from src.services.workflow_orchestrator import WorkflowOrchestrator

        await WorkflowOrchestrator().process_inbound_email(
            str(inbound_id), {"sender": ae_email, "subject": "Closed: Acme Corp", "body": CLOSED_BODY, "gmail_thread_id": "T-deal"}
        )
        return str(db("SELECT id FROM app.onboardings WHERE inbound_email_id = %s", (inbound_id,))[0]["id"])

    from main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield {"db": db, "client": client, "start_deal": start_deal}

    from src.config.database_config import db_pool

    await db_pool.close()
    _reset_singletons()
    admin.execute(f'DROP DATABASE "{db_name}" WITH (FORCE)')
    admin.close()


# ── helpers that send Bolna-shaped webhooks ───────────────────────────────


async def bolna(client, execution_id, status, token=WEBHOOK_TOKEN):
    """Bolna POSTs the execution payload on every status change."""
    return await client.post(
        f"/webhooks/bolna?token={token}",
        json={"id": execution_id, "agent_id": "agent-1", "status": status},
    )


def finish(execution_id, status, plan=None, readback=None, confidence=0.95, hangup_reason="call_ended"):
    """What Bolna's GET /executions/{id} returns once the call is over."""
    extracted = {}
    if plan is not None:
        extracted = {"Plan confirmation": {
            "plan_tier": {"objective": plan, "confidence": confidence},
            "readback_confirmed": {"objective": "yes" if readback else "no", "confidence": confidence},
        }}
    FakeBolna.executions[execution_id] = {
        "id": execution_id,
        "status": status,
        "conversation_duration": 41.5,
        "transcript": f"assistant: Hi... ({status})",
        "extracted_data": extracted,
        "telephony_data": {"recording_url": f"https://rec/{execution_id}.mp3", "hangup_reason": hangup_reason},
    }


def run_row(db, onboarding_id, workflow="tier_confirmation"):
    """The ``workflow_runs`` row of one part of an onboarding (tier_confirmation by default)."""
    return db("SELECT * FROM app.workflow_runs WHERE onboarding_id = %s AND workflow = %s", (onboarding_id, workflow))[0]


def waiting(run):
    """(status, waiting_for) — what a run is doing and why."""
    return run["status"], run["waiting_for"]


def onboarding_row(db, onboarding_id):
    return db("SELECT * FROM app.onboardings WHERE id = %s", (onboarding_id,))[0]


# ── scenarios ─────────────────────────────────────────────────────────────


async def test_no_answer_then_retry_then_confirmed(env):
    from src.services.retry_service import RetryService

    db, client = env["db"], env["client"]
    oid = await env["start_deal"]()

    # Email passed the analyser, call 1 placed, run paused waiting for the result
    assert len(FakeBolna.calls) == 1
    assert FakeBolna.calls[0]["phone"] == "+14155550123"
    assert FakeBolna.calls[0]["customer"] == "Acme Corp"
    assert waiting(run_row(db, oid)) == ("waiting", "call_result")
    assert onboarding_row(db, oid)["status"] == "awaiting_tier_confirmation"

    # Auth: wrong token rejected; unknown executions ignored
    assert (await bolna(client, "exec-1", "no-answer", token="wrong")).status_code == 401
    assert (await bolna(client, "exec-999", "completed")).status_code == 200

    # Intermediate status is tracked but doesn't resume anything
    await bolna(client, "exec-1", "ringing")
    assert db("SELECT status FROM app.voice_calls WHERE provider_call_id = 'exec-1'")[0]["status"] == "ringing"
    assert waiting(run_row(db, oid)) == ("waiting", "call_result")

    # AE doesn't pick up → retry scheduled, no second call yet
    finish("exec-1", "no-answer", hangup_reason="no_answer")
    assert (await bolna(client, "exec-1", "no-answer")).status_code == 200
    run = run_row(db, oid)
    assert waiting(run) == ("waiting", "call_retry") and run["due_at"] is not None
    assert len(FakeBolna.calls) == 1

    # Repeated terminal webhook is ignored
    await bolna(client, "exec-1", "no-answer")
    assert waiting(run_row(db, oid)) == ("waiting", "call_retry") and len(FakeBolna.calls) == 1

    # Scheduler: retry due → call 2
    assert await RetryService().run_due_once() == 1
    assert len(FakeBolna.calls) == 2
    assert waiting(run_row(db, oid)) == ("waiting", "call_result")
    assert await RetryService().run_due_once() == 0

    # Webhook claims "completed" but Bolna's API says it isn't final yet → nothing happens
    FakeBolna.executions["exec-2"] = {"id": "exec-2", "status": "call-disconnected"}
    await bolna(client, "exec-2", "completed")
    assert waiting(run_row(db, oid)) == ("waiting", "call_result")

    # Bolna API down → 502 so the webhook can be retried; nothing finalised
    finish("exec-2", "completed", plan="enterprise", readback=True)
    FakeBolna.api_down = True
    assert (await bolna(client, "exec-2", "completed")).status_code == 502
    FakeBolna.api_down = False
    assert onboarding_row(db, oid)["plan_tier"] is None

    # Final completed webhook → verified → graph resumes → Enterprise project
    await bolna(client, "exec-2", "completed")
    ob = onboarding_row(db, oid)
    assert (ob["plan_tier"], ob["plan_tier_source"]) == ("enterprise", "voice_call")
    assert ob["status"] == "project_created"
    assert run_row(db, oid)["status"] == "succeeded"
    assert run_row(db, oid, "provisioning")["status"] == "succeeded"       # tier -> provisioning hand-off
    assert len(FakeRocketlane.created) == 1
    assert FakeRocketlane.created[0]["template_id"] == "111"
    assert FakeRocketlane.created[0]["owner_email"] == "priya@novacrm.test"     # the CS manager owns the project

    calls = db("SELECT attempt_no, outcome, detected_plan_tier, readback_confirmed, confidence, recording_url, "
               "duration_seconds FROM app.voice_calls WHERE onboarding_id = %s ORDER BY attempt_no", (oid,))
    assert [(c["attempt_no"], c["outcome"]) for c in calls] == [(1, "no_answer"), (2, "confirmed")]
    assert calls[1]["detected_plan_tier"] == "enterprise" and calls[1]["readback_confirmed"] is True
    assert float(calls[1]["confidence"]) == 0.95
    assert calls[1]["recording_url"] == "https://rec/exec-2.mp3" and calls[1]["duration_seconds"] == 41

    # A late repeat after completion changes nothing
    await bolna(client, "exec-2", "completed")
    assert len(FakeRocketlane.created) == 1 and len(FakeBolna.calls) == 2

    events = [r["event_type"] for r in db(
        "SELECT event_type FROM app.audit_events WHERE onboarding_id = %s ORDER BY id", (oid,))]
    for expected in ["email_parsed", "call_started", "call_finished", "call_retry_scheduled",
                     "tier_confirmed", "project_created"]:
        assert expected in events


async def test_unconfirmed_readback_low_confidence_then_manual_tier(env):
    from src.services.retry_service import RetryService

    db, client = env["db"], env["client"]
    oid = await env["start_deal"]()

    # Attempt 1: AE said "enterprise" but didn't confirm the read-back → NOT accepted
    finish("exec-1", "completed", plan="enterprise", readback=False)
    await bolna(client, "exec-1", "completed")
    assert onboarding_row(db, oid)["plan_tier"] is None
    assert waiting(run_row(db, oid)) == ("waiting", "call_retry")

    # Attempt 2: confirmed, but the extraction confidence is too low → NOT accepted
    await RetryService().run_due_once()
    finish("exec-2", "completed", plan="growth", readback=True, confidence=0.4)
    await bolna(client, "exec-2", "completed")
    assert onboarding_row(db, oid)["plan_tier"] is None

    # Attempt 3: busy
    await RetryService().run_due_once()
    finish("exec-3", "busy")
    await bolna(client, "exec-3", "busy")

    assert len(FakeBolna.calls) == 3
    assert waiting(run_row(db, oid)) == ("waiting", "email_reply")
    ob = onboarding_row(db, oid)
    assert ob["status"] == "needs_human_review"
    assert "3 call attempt" in ob["status_reason"]
    outcomes = [r["outcome"] for r in db(
        "SELECT outcome FROM app.voice_calls WHERE onboarding_id = %s ORDER BY attempt_no", (oid,))]
    assert outcomes == ["unclear", "unclear", "no_answer"]

    # Manual override: auth required, then resumes the run
    url = f"/onboardings/{oid}/plan-tier"
    body = {"plan_tier": "growth", "reviewer": "priya@novacrm.test"}
    assert (await client.post(url, json=body, headers={"x-admin-token": "nope"})).status_code == 401
    assert (await client.post(url, json=body, headers={"x-admin-token": ADMIN_TOKEN})).status_code == 200

    ob = onboarding_row(db, oid)
    assert (ob["plan_tier"], ob["plan_tier_source"], ob["status"]) == ("growth", "manual_override", "project_created")
    assert FakeRocketlane.created[0]["template_id"] == "222"
    assert run_row(db, oid)["status"] == "succeeded"

    # Can't be applied twice
    assert (await client.post(url, json=body, headers={"x-admin-token": ADMIN_TOKEN})).status_code == 409


async def test_outside_working_hours_defers_call(env, monkeypatch):
    from src.services.retry_service import RetryService

    db = env["db"]
    monkeypatch.setattr(tier_nodes, "_now", lambda: datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc))  # 03:00 AE time
    oid = await env["start_deal"]()

    assert FakeBolna.calls == []
    run = run_row(db, oid)
    assert waiting(run) == ("waiting", "call_retry")
    assert run["due_at"] == datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)
    assert db("SELECT count(*) AS n FROM app.voice_calls WHERE onboarding_id = %s", (oid,))[0]["n"] == 0

    # When the window opens the scheduler places the first real call (attempt 1)
    monkeypatch.setattr(tier_nodes, "_now", lambda: MONDAY_10_UTC)
    assert await RetryService().run_due_once() == 1
    assert len(FakeBolna.calls) == 1
    assert db("SELECT attempt_no FROM app.voice_calls WHERE onboarding_id = %s", (oid,))[0]["attempt_no"] == 1


async def test_ae_without_phone_goes_straight_to_human(env):
    db = env["db"]
    oid = await env["start_deal"](ae_email="unknown-ae@novacrm.test")

    assert FakeBolna.calls == []
    assert waiting(run_row(db, oid)) == ("waiting", "email_reply")
    ob = onboarding_row(db, oid)
    assert ob["status"] == "needs_human_review"
    assert "No phone number" in ob["status_reason"]


# ── polling mode (default: no webhook / public URL needed) ─────────────────


async def test_polling_mode_end_to_end_without_any_webhook(env):
    from src.services.call_result_service import CallResultService
    from src.services.retry_service import RetryService

    db = env["db"]
    oid = await env["start_deal"]()
    assert len(FakeBolna.calls) == 1

    # Call still ringing → poller just tracks the status
    FakeBolna.executions["exec-1"] = {"id": "exec-1", "status": "ringing"}
    assert await CallResultService().poll_in_flight_once() == 0
    assert db("SELECT status FROM app.voice_calls WHERE provider_call_id = 'exec-1'")[0]["status"] == "ringing"
    assert waiting(run_row(db, oid)) == ("waiting", "call_result")

    # Bolna temporarily unreachable → skipped, tried again next pass
    FakeBolna.api_down = True
    assert await CallResultService().poll_in_flight_once() == 0
    FakeBolna.api_down = False
    assert waiting(run_row(db, oid)) == ("waiting", "call_result")

    # No answer → poller picks it up and the graph schedules a retry
    finish("exec-1", "no-answer")
    assert await CallResultService().poll_in_flight_once() == 1
    assert waiting(run_row(db, oid)) == ("waiting", "call_retry")
    assert await CallResultService().poll_in_flight_once() == 0     # finished calls aren't polled again

    # Retry → call 2 → confirmed → project created, all without a webhook
    await RetryService().run_due_once()
    assert len(FakeBolna.calls) == 2
    finish("exec-2", "completed", plan="growth", readback=True)
    assert await CallResultService().poll_in_flight_once() == 1
    ob = onboarding_row(db, oid)
    assert (ob["plan_tier"], ob["status"]) == ("growth", "project_created")
    assert FakeRocketlane.created[0]["template_id"] == "222"
    assert run_row(db, oid)["status"] == "succeeded"
    actors = {r["actor"] for r in db(
        "SELECT actor FROM app.audit_events WHERE onboarding_id = %s AND event_type = 'call_finished'", (oid,))}
    assert actors == {"poller"}


async def test_poller_and_webhook_together_process_a_call_once(env):
    from src.services.call_result_service import CallResultService

    db, client = env["db"], env["client"]
    oid = await env["start_deal"]()
    finish("exec-1", "completed", plan="enterprise", readback=True)

    await bolna(client, "exec-1", "completed")           # webhook wins the race
    assert await CallResultService().poll_in_flight_once() == 0        # poller finds nothing left to do
    assert len(FakeRocketlane.created) == 1
    assert onboarding_row(db, oid)["status"] == "project_created"


async def test_stuck_call_times_out_and_is_retried(env, monkeypatch):
    from src.config import settings as settings_module
    from src.services.call_result_service import CallResultService

    db = env["db"]
    oid = await env["start_deal"]()
    # Bolna never reports a final status
    FakeBolna.executions["exec-1"] = {"id": "exec-1", "status": "call-disconnected"}
    monkeypatch.setattr(settings_module.get_settings(), "voice_call_timeout_minutes", 0)

    assert await CallResultService().poll_in_flight_once() == 1
    call = db("SELECT outcome, ended_reason FROM app.voice_calls WHERE provider_call_id = 'exec-1'")[0]
    assert call["outcome"] == "failed" and call["ended_reason"] == "failed"
    assert waiting(run_row(db, oid)) == ("waiting", "call_retry")


async def test_the_tier_is_read_from_the_transcript_when_bolna_returns_no_extractions(env):
    """What happened in practice: the AE said "growth" and said "yes" to the read-back, but the Bolna agent
    had no extractions configured, so extracted_data came back null."""
    from src.services.call_result_service import CallResultService

    db = env["db"]
    oid = await env["start_deal"]()
    FakeBolna.executions["exec-1"] = {
        "id": "exec-1", "status": "completed", "conversation_duration": 37.0, "extracted_data": None,
        "transcript": ("assistant: Which plan is Acme Corp on, Enterprise or Growth?\nuser: growth\n"
                       "assistant: Just to confirm, Acme Corp is on the Growth plan, correct?\nuser: yes\n"),
        "telephony_data": {"recording_url": "https://rec/exec-1.mp3", "hangup_reason": "end_call_tool"},
    }

    assert await CallResultService().poll_in_flight_once() == 1
    ob = onboarding_row(db, oid)
    assert (ob["plan_tier"], ob["plan_tier_source"], ob["status"]) == ("growth", "voice_call", "project_created")
    assert FakeRocketlane.created[0]["template_id"] == "222"
    call = db("SELECT outcome, detected_plan_tier, readback_confirmed FROM app.voice_calls WHERE onboarding_id = %s", (oid,))[0]
    assert (call["outcome"], call["detected_plan_tier"], call["readback_confirmed"]) == ("confirmed", "growth", True)
    finished = db("SELECT payload FROM app.audit_events WHERE onboarding_id = %s AND event_type = 'call_finished'", (oid,))[0]
    assert finished["payload"]["source"] == "transcript"                       # the audit trail says where the tier came from


async def test_no_extractions_and_an_unclear_transcript_is_still_unclear(env):
    from src.services.call_result_service import CallResultService

    db = env["db"]
    oid = await env["start_deal"]()
    FakeBolna.executions["exec-1"] = {
        "id": "exec-1", "status": "completed", "extracted_data": None, "telephony_data": {"hangup_reason": "end_call_tool"},
        "transcript": "assistant: Which plan?\nuser: not sure, let me check\nassistant: Okay, bye.\n",
    }
    await CallResultService().poll_in_flight_once()
    assert onboarding_row(db, oid)["plan_tier"] is None
    assert waiting(run_row(db, oid)) == ("waiting", "call_retry")                 # retried, never guessed
