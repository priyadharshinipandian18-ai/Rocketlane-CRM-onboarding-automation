"""The models package (base model, entities, enums) and the service layer.

The first half needs nothing; the second half runs the services against a
throwaway real Postgres (see ``temp_database`` in conftest.py).
"""
import os
from datetime import date
from uuid import UUID, uuid4

os.environ.setdefault("GOOGLE_API_KEY", "test-key-not-used")

import pytest

from src.models.base_model import BaseModel
from src.models.entities import (
    AccountExecutive,
    AuditEvent,
    Mailbox,
    Onboarding,
    PlanTier,
    SlackChannel,
    VoiceCall,
    WorkflowRun,
)
from src.models.enums import OnboardingStatus, PlanTierCode
from src.services import (
    AccountExecutiveService,
    AuditEventService,
    BaseService,
    CsTeamMemberService,
    InboundEmailService,
    MailboxService,
    OnboardingService,
    PlanTierService,
    SlackChannelService,
    VoiceCallService,
    WorkflowRunService,
)


# ── models (no database) ────────────────────────────────────────────────────

def test_every_entity_maps_to_an_app_table():
    from src.models import entities

    for name in entities.__all__:
        entity = getattr(entities, name)
        assert issubclass(entity, BaseModel)
        assert entity.table_ref() == f"app.{entity.__table_name__}"


def test_non_table_models_refuse_a_table_ref():
    class Dto(BaseModel):
        x: int = 1

    with pytest.raises(TypeError):
        Dto.table_ref()


def test_from_row_stringifies_uuids_and_keeps_enums_as_plain_strings():
    row = {"id": uuid4(), "inbound_email_id": UUID(int=7), "customer_name": "Acme", "customer_contact_email": "j@a.co",
           "plan_tier": "enterprise", "status": "project_created", "unknown_column": "ignored"}
    onboarding = Onboarding.from_row(row)
    assert isinstance(onboarding.id, str) and onboarding.inbound_email_id == str(UUID(int=7))
    assert onboarding.plan_tier == "enterprise" and onboarding.status == OnboardingStatus.PROJECT_CREATED
    assert type(onboarding.status) is str            # use_enum_values
    assert Onboarding.from_row(None) is None


def test_invalid_enum_values_are_rejected():
    with pytest.raises(ValueError):
        PlanTier(code="platinum", display_name="x", rocketlane_template_id="1", duration_days=1,
                 csm_model="dedicated", slack_channel_prefix="p", welcome_message_template="t")


def test_plan_tier_is_keyed_by_code():
    assert PlanTier.__primary_key__ == "code" and Onboarding.__primary_key__ == "id"


def test_state_classes_are_typed_dicts_that_inherit_the_subgraph_states():
    from typing import is_typeddict, get_type_hints

    from src.agents.communication.state import CommunicationState
    from src.agents.email_analyser.state import EmailAnalyserState
    from src.agents.intake.state import IntakeState
    from src.agents.provisioning.state import ProvisioningState
    from src.agents.tier_confirmation.state import TierConfirmationState

    for state in (IntakeState, EmailAnalyserState, CommunicationState, ProvisioningState, TierConfirmationState):
        assert is_typeddict(state)
    # a workflow's state inherits the sub-agent state it embeds, so those keys flow back up
    assert set(get_type_hints(EmailAnalyserState)) <= set(get_type_hints(IntakeState))
    assert set(get_type_hints(CommunicationState)) <= set(get_type_hints(ProvisioningState))


# ── services (real Postgres) ────────────────────────────────────────────────

async def test_base_service_crud_roundtrip(temp_database):
    service = BaseService(AccountExecutive)
    saved = await service.save(AccountExecutive(name="Raj", email="raj@x.co", phone_e164="+14155550123", timezone="Asia/Kolkata"))
    assert saved.id and saved.active is True and saved.slack_user_id is None      # column defaults applied

    assert (await service.get_by_id(saved.id)).email == "raj@x.co"
    assert (await service.find_one(email="raj@x.co")).id == saved.id
    assert await service.find_one(email="nobody@x.co") is None

    updated = await service.update(saved.id, timezone="UTC", slack_user_id="U1")
    assert (updated.timezone, updated.slack_user_id) == ("UTC", "U1")

    await service.delete(saved.id)
    assert await service.get_by_id(saved.id) is None


async def test_base_service_rejects_unknown_columns(temp_database):
    service = BaseService(AccountExecutive)
    with pytest.raises(ValueError):
        await service.find_all(**{"email; DROP TABLE app.account_executives": 1})
    with pytest.raises(ValueError):
        await service.update("x", not_a_column=1)


async def test_json_columns_roundtrip_as_dicts(temp_database):
    await MailboxService().get_or_create("cs@x.co")
    mailbox = await MailboxService().get_or_create("cs@x.co")              # second call finds, doesn't duplicate
    assert len(await MailboxService().find_all()) == 1

    emails = InboundEmailService()
    inbound = await emails.try_insert(mailbox.id, "g1", None, "ae@x.co", "Closed", None)
    assert await emails.try_insert(mailbox.id, "g1", None, "ae@x.co", "Closed", None) is None   # replay is a no-op
    await emails.update_classification(inbound.id, True, {"confidence": "high"}, "parsed")
    stored = await emails.get_by_id(inbound.id)
    assert stored.classification == {"confidence": "high"} and stored.status == "parsed"


async def test_audit_events_keep_order_and_payload(temp_database):
    audit = AuditEventService()
    await audit.log_event(None, "tester", "first", {"n": 1})
    await audit.log_event(None, "tester", "second")
    events = await audit.find_all(order_by="id")
    assert [e.event_type for e in events] == ["first", "second"]
    assert events[0].payload == {"n": 1} and isinstance(events[0].id, int)


async def _seed_onboarding(sf_opportunity_id: str | None = None):
    mailbox = await MailboxService().get_or_create("cs@x.co")
    inbound = await InboundEmailService().try_insert(mailbox.id, uuid4().hex, None, "ae@x.co", "Closed", None)
    oid = str(uuid4())
    onboarding = await OnboardingService().create(
        oid, inbound.id, "Acme Corp", "jane@acme.co", "Jane", None, sf_opportunity_id or f"006{uuid4().hex[:8]}", {}
    )
    return oid, onboarding


def _seed_plan_tiers(db_uri: str) -> None:
    import psycopg

    with psycopg.connect(db_uri, autocommit=True) as conn:
        conn.execute("""
            INSERT INTO app.plan_tiers VALUES
              ('enterprise','Enterprise','111',30,'dedicated','nova-ent-','Hi {customer_name}', now()),
              ('growth','Growth','222',14,'pooled','nova-','Hi {customer_name}', now());
        """)


async def test_onboarding_lifecycle_and_listing(temp_database):
    _seed_plan_tiers(temp_database)
    service = OnboardingService()
    oid, onboarding = await _seed_onboarding()
    assert onboarding.status == "received" and onboarding.plan_tier is None

    await service.set_plan_tier(oid, PlanTierCode.GROWTH, source="manual_override")
    current = await service.get_by_id(oid)
    assert (current.plan_tier, current.plan_tier_source, current.status) == ("growth", "manual_override", "tier_confirmed")

    await service.update_status(oid, OnboardingStatus.NEEDS_HUMAN_REVIEW, "because")
    assert [o.id for o in await service.list_by_status("needs_human_review")] == [oid]
    assert await service.list_by_status(OnboardingStatus.COMPLETED) == []
    assert len(await service.list_by_status()) == 1

    with pytest.raises(ValueError):
        await service.update_status(oid, "not-a-status")


async def test_duplicate_opportunity_is_rejected_by_create(temp_database):
    _, first = await _seed_onboarding("006SAME")
    _, again = await _seed_onboarding("006SAME")      # the AE re-sent the same deal
    assert first is not None and again is None


def _past(minutes: int = 1):
    from datetime import datetime, timedelta, timezone

    return datetime.now(timezone.utc) - timedelta(minutes=minutes)


async def test_workflow_runs_are_one_per_workflow_and_idempotent(temp_database):
    oid, _ = await _seed_onboarding()
    runs = WorkflowRunService()
    tier = await runs.create("tier_confirmation", f"tier:{oid}", onboarding_id=oid)
    assert (tier.status, tier.workflow, tier.thread_id, tier.waiting_for) == ("pending", "tier_confirmation", f"tier:{oid}", None)
    assert (await runs.create("tier_confirmation", f"tier:{oid}", onboarding_id=oid)).id == tier.id      # same thread: same run

    await runs.create("provisioning", f"provision:{oid}", onboarding_id=oid)
    assert [r.workflow for r in await runs.list_for_onboarding(oid)] == ["tier_confirmation", "provisioning"]
    assert (await runs.get_for(oid, "provisioning")).thread_id == f"provision:{oid}"
    with pytest.raises(Exception):                      # a second tier run for the same onboarding is refused
        await runs.create("tier_confirmation", f"tier:{oid}:2", onboarding_id=oid)


async def test_a_waiting_run_always_says_why_and_events_claim_it_once(temp_database):
    oid, _ = await _seed_onboarding()
    runs = WorkflowRunService()
    await runs.create("tier_confirmation", f"tier:{oid}", onboarding_id=oid, status="running")

    await runs.mark_waiting(oid, "tier_confirmation", "call_result", current_node="await_plan_tier_result")
    parked = await runs.get_for(oid, "tier_confirmation")
    assert (parked.status, parked.waiting_for, parked.current_node) == ("waiting", "call_result", "await_plan_tier_result")

    assert await runs.claim_waiting(oid, "tier_confirmation", "email_reply") is None     # a stray event for another wait
    claimed = await runs.claim_waiting(oid, "tier_confirmation", "call_result")
    assert (claimed.status, claimed.waiting_for) == ("running", None)
    assert await runs.claim_waiting(oid, "tier_confirmation", "call_result") is None     # already claimed


async def test_call_retries_are_claimed_when_due_and_only_once(temp_database):
    oid, _ = await _seed_onboarding()
    runs = WorkflowRunService()
    await runs.create("tier_confirmation", f"tier:{oid}", onboarding_id=oid, status="running")

    from datetime import datetime, timedelta, timezone
    await runs.mark_waiting(oid, "tier_confirmation", "call_retry", "wait_for_call_retry", due_at=datetime.now(timezone.utc) + timedelta(hours=1))
    assert await runs.claim_due_call_retries() == []                                     # not due yet
    await runs.mark_waiting(oid, "tier_confirmation", "call_retry", "wait_for_call_retry", due_at=_past())
    assert [r.onboarding_id for r in await runs.claim_due_call_retries()] == [oid]
    assert await runs.claim_due_call_retries() == []


async def test_a_run_that_waited_too_long_is_stopped_and_a_late_answer_still_continues_it(temp_database):
    oid, _ = await _seed_onboarding()
    runs = WorkflowRunService()
    await runs.create("tier_confirmation", f"tier:{oid}", onboarding_id=oid, status="running")

    from datetime import datetime, timedelta, timezone
    await runs.mark_waiting(oid, "tier_confirmation", "email_reply", "await_ae_reply", stop_at=datetime.now(timezone.utc) + timedelta(hours=1))
    assert await runs.claim_due_stops() == []
    await runs.mark_waiting(oid, "tier_confirmation", "email_reply", "await_ae_reply", stop_at=_past())

    stopped = await runs.claim_due_stops()
    assert [(r.status, r.waiting_for, r.current_node) for r in stopped] == [("stopped", None, "await_ae_reply")]
    assert await runs.claim_due_stops() == []

    reopened = await runs.reopen_stopped(oid, datetime.now(timezone.utc) + timedelta(hours=24))
    assert (reopened.status, reopened.waiting_for) == ("waiting", "email_reply")
    assert await runs.reopen_stopped(oid, datetime.now(timezone.utc)) is None            # only a stopped run reopens

    await runs.claim_due_stops()                                                         # not due: unchanged
    await runs.mark_waiting(oid, "tier_confirmation", "email_reply", "await_ae_reply", stop_at=_past())
    await runs.claim_due_stops()
    assert (await runs.claim_for_tier_answer(oid)).status == "running"                   # a late answer claims a STOPPED run
    assert await runs.claim_for_tier_answer(oid) is None


async def test_pending_handoffs_are_claimed_and_intake_runs_never_are(temp_database):
    oid, _ = await _seed_onboarding()
    runs = WorkflowRunService()
    await runs.create("provisioning", f"provision:{oid}", onboarding_id=oid)             # pending
    await runs.create("intake", "intake:abc", status="pending")                          # not started via pending
    claimed = await runs.claim_pending()
    assert [(r.workflow, r.status) for r in claimed] == [("provisioning", "running")]
    assert await runs.claim_pending() == []


async def test_a_failed_run_is_retried_manually_and_counts_the_attempt(temp_database):
    oid, _ = await _seed_onboarding()
    runs = WorkflowRunService()
    run = await runs.create("provisioning", f"provision:{oid}", onboarding_id=oid, status="running")

    assert await runs.claim_failed(oid, "provisioning") is None                          # only a failed run
    await runs.mark_failed(run.id, "RocketlaneDown: 503", current_node="create_rocketlane_project")
    failed = await runs.get_for(oid, "provisioning")
    assert (failed.status, failed.current_node, failed.last_error) == ("failed", "create_rocketlane_project", "RocketlaneDown: 503")

    retried = await runs.claim_failed(oid, "provisioning")
    assert (retried.status, retried.attempt_count) == ("running", 1)
    assert await runs.claim_failed(oid, "provisioning") is None

    await runs.mark_succeeded(run.id, "project+channel")
    done = await runs.get_for(oid, "provisioning")
    assert (done.status, done.outcome, done.last_error) == ("succeeded", "project+channel", None) and done.finished_at


async def test_escalations_supersede_each_other_and_match_replies_by_thread_then_token(temp_database):
    from src.services import EscalationService

    oid, _ = await _seed_onboarding()
    run = await WorkflowRunService().create("tier_confirmation", f"tier:{oid}", onboarding_id=oid, status="running")
    escalations = EscalationService()

    first = await escalations.create_attempt(oid, run.id, "ONB-abcd1234", "ae@x.co")
    await escalations.set_sent(first.id, "gm-1", "thread-1")
    assert first.attempt_no == 1 and first.status == "sent"

    second = await escalations.create_attempt(oid, run.id, "ONB-abcd1234", "ae@x.co")   # the request was re-opened
    await escalations.set_sent(second.id, "gm-2", "thread-2")
    assert second.attempt_no == 2
    assert (await escalations.get_by_id(first.id)).status == "superseded"
    assert (await escalations.latest_for(oid)).id == second.id

    by_thread, source = await escalations.match_reply("thread-2", "unrelated subject")
    assert (by_thread.id, source.value) == (second.id, "thread_match")
    by_token, source = await escalations.match_reply("some-other-thread", "Re: [ONB-abcd1234] Plan tier needed")
    assert (by_token.id, source.value) == (second.id, "token")
    assert await escalations.match_reply("some-other-thread", "Closed: Acme") is None
    assert await escalations.match_reply(None, None) is None

    await escalations.mark_answered(oid, "growth", "ae@x.co")
    answered = await escalations.get_by_id(second.id)
    assert (answered.status, answered.answered_tier, answered.answered_by) == ("answered", "growth", "ae@x.co")


async def test_voice_call_attempts_are_idempotent(temp_database):
    oid, _ = await _seed_onboarding()
    calls = VoiceCallService()
    first = await calls.create_attempt(oid, 1, "bolna", "+14155550123")
    again = await calls.create_attempt(oid, 1, "bolna", "+14155550123")
    assert first.id == again.id and again.status == "queued"

    await calls.set_provider_call_id(first.id, "exec-1")
    assert [c.id for c in await calls.list_in_flight("bolna")] == [first.id]
    done = await calls.finalize_once("exec-1", "completed", "hi", None, 41, {"id": "exec-1"})
    assert done.status == "completed" and await calls.finalize_once("exec-1", None, None, None, None, {}) is None
    assert await calls.list_in_flight("bolna") == []


async def test_comms_context_joins_tier_team_and_project(temp_database):
    import psycopg

    oid, _ = await _seed_onboarding()
    _seed_plan_tiers(temp_database)
    with psycopg.connect(temp_database, autocommit=True) as conn:
        conn.execute("""
            INSERT INTO app.cs_team_members (name,email,role,pool,rocketlane_user_id,slack_user_id)
              VALUES ('Sam','sam@x.co','csm','enterprise','rl-1','U_SAM');
        """)
    csm, manager = await CsTeamMemberService().get_owner_and_manager("enterprise")
    assert csm.name == "Sam" and csm.role == "csm" and manager is None

    onboardings = OnboardingService()
    assert await onboardings.get_comms_context(oid) is None            # no tier confirmed yet
    await onboardings.set_plan_tier(oid, "enterprise")
    await onboardings.set_team_and_dates(oid, csm.id, None, date(2026, 10, 7), date(2026, 11, 6))

    tier = await PlanTierService().get_by_code("enterprise")
    assert tier.duration_days == 30 and tier.csm_model == "dedicated"
    ctx = await onboardings.get_comms_context(oid)
    assert (ctx.tier_name, ctx.csm_name, ctx.csm_slack_user_id, ctx.manager_name) == ("Enterprise", "Sam", "U_SAM", None)
    assert ctx.target_go_live_date == date(2026, 11, 6) and ctx.csm_model == "dedicated"


async def test_slack_channel_service_tracks_creation_and_welcome(temp_database):
    oid, _ = await _seed_onboarding()
    channels = SlackChannelService()
    assert await channels.get_by_onboarding(oid) is None
    pending = await channels.create_pending(oid, "nova-ent-acme-corp", "topic")
    assert pending.status == "creating" and pending.slack_channel_id is None

    await channels.mark_created(oid, "C123", "nova-ent-acme-corp")
    await channels.set_welcome_ts(oid, "1700000000.0001")
    await channels.set_invite(oid, "I1", "sent")
    stored = await channels.get_by_onboarding(oid)
    assert (stored.slack_channel_id, stored.status, stored.welcome_message_ts, stored.invite_status) == (
        "C123", "created", "1700000000.0001", "sent")
    assert isinstance(stored, SlackChannel)
