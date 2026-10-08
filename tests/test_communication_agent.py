"""Communication-agent tests — no live Slack, LLM or Postgres.

Personalisation and the welcome guardrails are pure functions; the ReAct agent runs end to end with a
scripted LLM, a fake ``SlackClient`` and in-memory stand-ins for the DB repos.
"""
import itertools
import os
from datetime import date
from types import SimpleNamespace

os.environ.setdefault("GOOGLE_API_KEY", "test-key-not-used")

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from src.agents.communication import nodes, operations
from src.agents.communication.channel_plan import (
    DEFAULT_WELCOME_TEMPLATE,
    build_channel_plan,
    channel_name_candidates,
    slugify,
)
from src.agents.communication.graph import build_communication_graph
from src.agents.communication.operations import OpError, validate_channel_name
from src.agents.communication.context import window
from src.agents.communication.tools import TOOLS, create_slack_channel, invite_customer_to_channel, post_welcome_message
from src.agents.communication.welcome import check_welcome, render_welcome
from src.integrations.slack_client import SlackApiError
from src.models.dto.comms_context import OnboardingCommsContext
from src.models.entities import SlackChannel
from src.models.enums import OnboardingStatus, SlackInviteStatus
from src.services import AuditEventService, OnboardingService, SlackChannelService

ONBOARDING_ID = "7f3c2a10-0000-4000-8000-000000000001"


def _ctx(**overrides) -> OnboardingCommsContext:
    base = {
        "id": ONBOARDING_ID,
        "customer_name": "Acme Corp",
        "customer_contact_name": "Jane Doe",
        "customer_contact_email": "jane@acme.com",
        "tier_name": "Enterprise",
        "duration_days": 30,
        "csm_model": "dedicated",
        "slack_channel_prefix": "nova-ent-",
        "welcome_message_template": "",
        "start_date": date(2026, 10, 7),
        "target_go_live_date": date(2026, 11, 6),
        "csm_name": "Sam CSM",
        "csm_slack_user_id": "U_CSM",
        "manager_name": "Mia Manager",
        "manager_slack_user_id": "U_MGR",
        "ae_name": "Alex AE",
        "ae_slack_user_id": "U_AE",
        "project_url": "https://rocketlane.example/p/1",
    }
    return OnboardingCommsContext(**{**base, **overrides})


# ── personalisation (pure) ──────────────────────────────────────────────────

def test_slugify_is_slack_safe():
    assert slugify("Acme Corp.") == "acme-corp"
    assert slugify("Zoë & Sons, Ltd") == "zoe-sons-ltd"
    assert slugify("!!!") == "customer"


def test_channel_name_uses_tier_prefix_and_fits_slack_limit():
    primary, fallback = channel_name_candidates("nova-ent-", "A" * 200, ONBOARDING_ID)
    assert primary.startswith("nova-ent-a") and len(primary) <= 80
    assert len(fallback) <= 80 and fallback.endswith("7f3c2a")
    assert channel_name_candidates("nova-", "Acme Corp", ONBOARDING_ID)[0] == "nova-acme-corp"


def test_enterprise_vs_growth_are_personalised_differently():
    ent = build_channel_plan(_ctx(), ONBOARDING_ID)
    growth = build_channel_plan(
        _ctx(tier_name="Growth", duration_days=14, csm_model="pooled", slack_channel_prefix="nova-"), ONBOARDING_ID
    )
    assert ent.name_candidates[0] == "nova-ent-acme-corp"
    assert growth.name_candidates[0] == "nova-acme-corp"
    assert "Enterprise onboarding" in ent.topic and "30-day" in ent.topic and "Dedicated CSM: Sam CSM" in ent.topic
    assert "Growth onboarding" in growth.topic and "14-day" in growth.topic and "CS lead: Sam CSM" in growth.topic
    assert "dedicated CSM" in ent.welcome_text and "pooled CS team" in growth.welcome_text
    assert "30-day" in ent.welcome_text and "14-day" in growth.welcome_text


def test_default_welcome_covers_phases_kickoff_and_escalation():
    text = build_channel_plan(_ctx(), ONBOARDING_ID).welcome_text
    for phase in ("Kickoff", "Data Migration", "Configuration", "Go-Live"):
        assert phase in text
    assert "Jane Doe" in text and "Acme Corp" in text and "November 6, 2026" in text
    assert "<@U_CSM>" in text and "<@U_MGR>" in text
    assert "confirm your migrated data" in text and "https://rocketlane.example/p/1" in text


def test_db_template_is_used_with_placeholders():
    plan = build_channel_plan(
        _ctx(welcome_message_template="Hi {contact_name} from {customer_name}! {unknown_key} {csm_mention}"),
        ONBOARDING_ID,
    )
    assert plan.welcome_text == "Hi Jane Doe from Acme Corp! {unknown_key} <@U_CSM>"


def test_blank_or_broken_template_falls_back_to_default():
    blank = build_channel_plan(_ctx(welcome_message_template="  "), ONBOARDING_ID).welcome_text
    broken = build_channel_plan(_ctx(welcome_message_template="Hello {customer_name"), ONBOARDING_ID).welcome_text
    expected = build_channel_plan(_ctx(), ONBOARDING_ID).welcome_text
    assert blank == expected == broken
    assert DEFAULT_WELCOME_TEMPLATE.startswith(":wave:")


def test_missing_slack_ids_degrade_to_names():
    plan = build_channel_plan(_ctx(csm_slack_user_id=None, manager_slack_user_id=None, ae_slack_user_id=None), ONBOARDING_ID)
    assert plan.missing_csm_slack_id is True
    assert plan.internal_user_ids == []
    assert "Sam CSM" in plan.welcome_text and "Mia Manager" in plan.welcome_text


def test_internal_user_ids_are_deduplicated():
    plan = build_channel_plan(_ctx(manager_slack_user_id="U_CSM"), ONBOARDING_ID)
    assert plan.internal_user_ids == ["U_CSM", "U_AE"]


# ── the ReAct agent end to end (scripted LLM + fake Slack + in-memory DB repos) ─────────────────────────

class FakeSlack:
    """Records calls. ``fail[method]`` is a list of error codes raised once each, in order (temporary
    errors); ``always[method]`` is an error raised on every call (permanent errors)."""

    instances: list = []
    taken_names: set[str] = set()
    fail: dict[str, list[str]] = {}
    always: dict[str, str] = {}
    members: dict[str, str] = {}               # email -> Slack member ID

    def __init__(self):
        self.calls: list[tuple] = []
        FakeSlack.instances.append(self)

    def _maybe_fail(self, method):
        if FakeSlack.fail.get(method):
            raise SlackApiError(method, FakeSlack.fail[method].pop(0))
        if method in FakeSlack.always:
            raise SlackApiError(method, FakeSlack.always[method])

    async def create_channel(self, name):
        self.calls.append(("create_channel", name))
        if name in FakeSlack.taken_names:
            raise SlackApiError("conversations.create", "name_taken")
        self._maybe_fail("create_channel")
        return "C123"

    async def set_topic(self, channel_id, topic):
        self.calls.append(("set_topic", channel_id, topic))
        self._maybe_fail("set_topic")

    async def invite_users(self, channel_id, user_ids):
        self.calls.append(("invite_users", channel_id, user_ids))
        self._maybe_fail("invite_users")

    async def post_message(self, channel_id, text):
        self.calls.append(("post_message", channel_id, text))
        self._maybe_fail("post_message")
        return "1700000000.000100"

    async def invite_shared(self, channel_id, email):
        self.calls.append(("invite_shared", channel_id, email))
        self._maybe_fail("invite_shared")
        return "I123"

    async def lookup_user_id_by_email(self, email):
        self.calls.append(("lookup_user_id_by_email", email))
        self._maybe_fail("lookup_user_id_by_email")
        return FakeSlack.members.get(email)


class ScriptedLLM(BaseChatModel):
    """A chat model that replays a script of AI messages (tool calls), and records what it was shown."""

    script: list = Field(default_factory=list)
    index: int = 0
    seen: list = Field(default_factory=list)
    fail_first: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(list(messages))
        if self.fail_first > 0:
            self.fail_first -= 1
            raise RuntimeError("LLM is down")
        message = self.script[self.index] if self.index < len(self.script) else AIMessage(content="All done.")
        self.index += 1
        return ChatResult(generations=[ChatGeneration(message=message)])


_call_counter = itertools.count(1)


def call(tool_name: str, /, **args) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": tool_name, "args": args, "id": f"call-{next(_call_counter)}", "type": "tool_call"}])


WELCOME = (
    "Welcome, Jane, and welcome to the Acme Corp onboarding! You are on our *Enterprise* plan: a 30-day onboarding "
    "with go-live targeted for November 6, 2026. {{csm}} is your dedicated CSM and {{manager}} is our CS manager. "
    "We start with Kickoff, then Data Migration, Configuration and Go-Live."
)

FULL_RUN = [
    "lookup_team_in_slack", "create_slack_channel", "set_channel_topic", "invite_internal_team", "post_welcome_message",
    "invite_customer_to_channel",
]


def full_script() -> list[AIMessage]:
    args = {
        "create_slack_channel": {"name": "nova-ent-acme-corp"},
        "set_channel_topic": {"topic": "Enterprise onboarding · Acme Corp · go-live Nov 6, 2026"},
        "post_welcome_message": {"text": WELCOME},
    }
    return [call(name, **args.get(name, {})) for name in FULL_RUN] + [AIMessage(content="The channel is ready.")]


@pytest.fixture
def env(monkeypatch):
    FakeSlack.instances, FakeSlack.taken_names, FakeSlack.fail, FakeSlack.always, FakeSlack.members = [], set(), {}, {}, {}
    db = SimpleNamespace(row=None, status=None, reason=None, audit=[], ctx=_ctx(), llm=ScriptedLLM(script=full_script()))

    async def get_ctx(self, _id):
        return db.ctx

    async def update_status(self, _id, status, reason=None):
        db.status, db.reason = OnboardingStatus(status).value, reason

    async def log_event(self, _id, actor, event_type, payload=None):
        db.audit.append(event_type)

    async def get_by_onboarding(self, _id):
        return db.row

    async def create_pending(self, _id, name, topic):
        db.row = SlackChannel(onboarding_id=ONBOARDING_ID, channel_name=name, topic=topic)
        return db.row

    async def mark_created(self, _id, channel_id, name):
        db.row = db.row.model_copy(update={"slack_channel_id": channel_id, "channel_name": name, "status": "created"})

    async def set_welcome_ts(self, _id, ts):
        db.row = db.row.model_copy(update={"welcome_message_ts": ts})

    async def set_invite(self, _id, invite_id, status):
        db.row = db.row.model_copy(update={"shared_invite_id": invite_id, "invite_status": SlackInviteStatus(status).value})

    async def mark_failed(self, _id, error):
        base = db.row or SlackChannel(onboarding_id=ONBOARDING_ID, channel_name="?")
        db.row = base.model_copy(update={"status": "failed", "error": error})

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(nodes, "get_settings", lambda: SimpleNamespace(slack_bot_token="xoxb-test", llm_attempts=2, communication_agent_max_steps=14))
    monkeypatch.setattr(nodes, "get_agent_llm", lambda: db.llm)
    monkeypatch.setattr(operations, "get_settings", lambda: SimpleNamespace(slack_tool_retry_attempts=3, slack_tool_retry_base_seconds=0))
    monkeypatch.setattr(operations, "_sleep", no_sleep)
    monkeypatch.setattr(operations, "SlackClient", FakeSlack)
    monkeypatch.setattr(OnboardingService, "get_comms_context", get_ctx)
    monkeypatch.setattr(OnboardingService, "update_status", update_status)
    monkeypatch.setattr(AuditEventService, "log_event", log_event)
    for name, fn in (
        ("get_by_onboarding", get_by_onboarding), ("create_pending", create_pending), ("mark_created", mark_created),
        ("set_welcome_ts", set_welcome_ts), ("set_invite", set_invite), ("mark_failed", mark_failed),
    ):
        monkeypatch.setattr(SlackChannelService, name, fn)
    return db


def _input():
    return {
        "onboarding_id": ONBOARDING_ID,
        "plan_tier": "enterprise",
        "rocketlane_project_id": "RL-1",
        "customer_contact_name": "Jane Doe",
        "customer_contact_email": "jane@acme.com",
    }


def _calls(method):
    return [c for inst in FakeSlack.instances for c in inst.calls if c[0] == method]


def _tool_results(llm: ScriptedLLM) -> list[str]:
    """Every tool result the model was shown by its last turn."""
    return [m.content for m in llm.seen[-1] if isinstance(m, ToolMessage)]


async def test_the_agent_sets_up_the_channel_with_an_llm_written_welcome(env):
    out = await build_communication_graph().ainvoke(_input())

    assert out["slack_channel_id"] == "C123" and out["slack_channel_name"] == "nova-ent-acme-corp"
    assert out["slack_invite_status"] == "sent"
    assert _calls("create_channel") == [("create_channel", "nova-ent-acme-corp")]
    assert "Enterprise onboarding" in _calls("set_topic")[0][2]
    assert _calls("invite_users")[0][2] == ["U_CSM", "U_MGR", "U_AE"]
    welcome = _calls("post_message")
    assert len(welcome) == 1 and "Jane" in welcome[0][2]                                   # the model's text is what is posted
    assert "<@U_CSM>" in welcome[0][2] and "<@U_MGR>" in welcome[0][2] and "{{" not in welcome[0][2]   # placeholders → real mentions
    assert _calls("invite_shared") == [("invite_shared", "C123", "jane@acme.com")]       # the address came from the DB
    assert env.status == "channel_created" and env.audit == ["slack_channel_created"]
    assert env.row.welcome_message_ts and env.row.invite_status == "sent"
    assert out["node_reports"][-1].status == "CHANNEL_CREATED"
    assert "unavailable" not in out["node_reports"][-1].reasoning


async def test_the_model_is_never_shown_emails_or_slack_ids(env):
    env.ctx = _ctx(csm_email="csm@x.test", manager_email="mgr@x.test", ae_email="ae@x.test")
    await build_communication_graph().ainvoke(_input())
    shown = "\n".join(str(m.content) for turn in env.llm.seen for m in turn)
    for secret in ("jane@acme.com", "csm@x.test", "mgr@x.test", "ae@x.test", "U_CSM", "U_MGR", "U_AE"):
        assert secret not in shown
    assert "Acme Corp" in shown and "nova-ent-acme-corp" in shown                          # the facts it does need are there


async def test_a_taken_name_is_recovered_by_the_agent_using_the_fallback_option(env):
    FakeSlack.taken_names = {"nova-ent-acme-corp"}
    script = full_script()
    script.insert(2, call("create_slack_channel", name="nova-ent-acme-corp-7f3c2a"))
    env.llm = ScriptedLLM(script=script)

    out = await build_communication_graph().ainvoke(_input())

    assert [c[1] for c in _calls("create_channel")] == ["nova-ent-acme-corp", "nova-ent-acme-corp-7f3c2a"]
    assert out["slack_channel_name"] == "nova-ent-acme-corp-7f3c2a" and env.status == "channel_created"
    assert any("name_taken" in r for r in _tool_results(env.llm))                          # the model was told what to do


async def test_a_temporary_slack_error_is_retried_inside_the_tool(env):
    FakeSlack.fail = {"create_channel": ["ratelimited", "internal_error"]}
    out = await build_communication_graph().ainvoke(_input())
    assert len(_calls("create_channel")) == 3 and out["slack_channel_id"] == "C123"       # 2 failures, 3rd attempt works
    assert env.status == "channel_created"


async def test_a_temporary_error_that_keeps_happening_is_reported_to_the_model_and_the_run_flagged(env):
    FakeSlack.fail = {"create_channel": ["ratelimited"] * 3}
    out = await build_communication_graph().ainvoke(_input())
    assert len(_calls("create_channel")) == 3                                              # gave up after 3 tries
    assert any(r.startswith("ERROR (temporary") for r in _tool_results(env.llm))
    assert out.get("slack_channel_id") is None
    assert env.status == "needs_human_review" and "ratelimited" in env.reason
    assert _calls("post_message") == [] and _calls("invite_shared") == []


async def test_a_permanent_slack_error_is_not_retried(env):
    FakeSlack.always = {"create_channel": "restricted_action"}
    out = await build_communication_graph().ainvoke(_input())
    assert len(_calls("create_channel")) == 1                                              # no retry, and no second try in finalize
    assert any(r.startswith("ERROR (permanent: restricted_action)") for r in _tool_results(env.llm))
    assert out.get("slack_channel_id") is None
    assert env.status == "needs_human_review" and "restricted_action" in env.reason and env.row.status == "failed"


async def test_a_welcome_that_breaks_the_rules_is_rejected_and_the_model_fixes_it(env):
    script = full_script()
    bad = call("post_welcome_message", text="Hello everyone @channel, welcome aboard to the shared onboarding channel for Acme!")
    script.insert(4, bad)                                                                  # before the good welcome
    env.llm = ScriptedLLM(script=script)

    await build_communication_graph().ainvoke(_input())

    posted = _calls("post_message")
    assert len(posted) == 1 and "@channel" not in posted[0][2]                             # only the good one went out
    assert any(r.startswith("REJECTED") and "@channel" in r for r in _tool_results(env.llm))


async def test_teammates_without_a_stored_slack_id_are_found_by_email(env):
    env.ctx = _ctx(csm_slack_user_id=None, csm_email="csm@x.test", ae_slack_user_id=None, ae_email="ae@x.test")
    FakeSlack.members = {"csm@x.test": "U_FOUND", "ae@x.test": "U_AE2"}
    await build_communication_graph().ainvoke(_input())
    assert _calls("invite_users")[0][2] == ["U_FOUND", "U_MGR", "U_AE2"] or set(_calls("invite_users")[0][2]) == {"U_FOUND", "U_MGR", "U_AE2"}
    assert "<@U_FOUND>" in _calls("post_message")[0][2]
    assert [c[1] for c in _calls("lookup_user_id_by_email")] == ["csm@x.test", "ae@x.test"]     # a stored ID is never looked up


async def test_a_csm_who_is_not_in_slack_is_reported_but_the_channel_is_still_created(env):
    env.ctx = _ctx(csm_slack_user_id=None, csm_email="ghost@x.test")
    out = await build_communication_graph().ainvoke(_input())
    assert out["slack_channel_id"] == "C123"
    assert any("CSM could not be added" in w for w in out["slack_warnings"]) and env.status == "needs_human_review"


async def test_a_failing_lookup_never_blocks_the_channel(env):
    env.ctx = _ctx(csm_slack_user_id=None, csm_email="csm@x.test")
    FakeSlack.always = {"lookup_user_id_by_email": "missing_scope"}
    out = await build_communication_graph().ainvoke(_input())
    assert out["slack_channel_id"] == "C123"
    assert any("Could not look up csm csm@x.test in Slack (missing_scope)" in w for w in out["slack_warnings"])


async def test_when_the_llm_is_down_the_standard_flow_still_completes_the_setup(env):
    env.llm = ScriptedLLM(script=[], fail_first=10)
    out = await build_communication_graph().ainvoke(_input())

    assert env.llm.index == 0 and len(env.llm.seen) == 2                                   # llm_attempts=2, both failed
    assert out["slack_channel_id"] == "C123" and env.status == "channel_created"
    assert _calls("create_channel")[0][1] == "nova-ent-acme-corp"
    assert "Jane Doe" in _calls("post_message")[0][2] and "Kickoff" in _calls("post_message")[0][2]   # the template welcome
    assert _calls("invite_shared") and "LLM was unavailable" in out["node_reports"][-1].reasoning


async def test_whatever_the_model_skips_is_completed_by_the_finalizer(env):
    env.llm = ScriptedLLM(script=[call("create_slack_channel", name="nova-ent-acme-corp")])     # then it just stops
    out = await build_communication_graph().ainvoke(_input())
    assert _calls("set_topic") and _calls("invite_users") and _calls("invite_shared")
    assert len(_calls("post_message")) == 1 and out["slack_invite_status"] == "sent" and env.status == "channel_created"


async def test_the_step_limit_stops_a_model_that_loops(env, monkeypatch):
    monkeypatch.setattr(
        nodes, "get_settings", lambda: SimpleNamespace(slack_bot_token="xoxb-test", llm_attempts=1, communication_agent_max_steps=3)
    )
    env.llm = ScriptedLLM(script=[call("lookup_team_in_slack") for _ in range(30)])
    out = await build_communication_graph().ainvoke(_input())
    assert len(env.llm.seen) == 3                                                          # cut off at the limit
    assert out["slack_channel_id"] == "C123" and env.status == "channel_created"             # and still finished


async def test_a_rerun_reuses_everything_and_never_repeats_a_message_or_invite(env):
    await build_communication_graph().ainvoke(_input())
    FakeSlack.instances.clear()
    env.llm = ScriptedLLM(script=full_script())

    out = await build_communication_graph().ainvoke(_input())

    assert _calls("create_channel") == [] and _calls("post_message") == [] and _calls("invite_shared") == []
    assert out["slack_channel_id"] == "C123" and env.status == "channel_created"


async def test_a_failed_customer_invite_is_flagged_and_the_csm_is_told_in_the_channel(env):
    FakeSlack.always = {"invite_shared": "not_allowed_token_type"}
    out = await build_communication_graph().ainvoke(_input())
    assert out["slack_invite_status"] == "failed" and env.status == "needs_human_review"
    assert len([c for c in _calls("post_message") if "invite them manually" in c[2]]) == 1
    assert len(_calls("invite_shared")) == 1                                               # permanent: not retried, not repeated


async def test_a_warning_from_an_earlier_step_flags_the_onboarding_for_review_even_if_slack_is_clean(env):
    """E.g. the Rocketlane template import was skipped: Slack still opens, and the onboarding ends flagged."""
    warning = "Rocketlane template 222 was NOT imported into project 4242 (RocketlaneError); import it manually in Rocketlane"
    out = await build_communication_graph().ainvoke({**_input(), "slack_warnings": [warning]})
    assert out["slack_channel_id"] == "C123"                                              # the channel was still created
    assert env.status == "needs_human_review" and "NOT imported into project 4242" in env.reason


async def test_an_onboarding_without_a_confirmed_tier_is_flagged_not_crashed(env):
    env.ctx = None
    out = await build_communication_graph().ainvoke(_input())
    assert out.get("slack_channel_id") is None and env.status == "needs_human_review"
    assert FakeSlack.instances == [] or all(not i.calls for i in FakeSlack.instances)


async def test_missing_token_skips_without_touching_anything(env, monkeypatch):
    monkeypatch.setattr(nodes, "get_settings", lambda: SimpleNamespace(slack_bot_token=""))
    out = await build_communication_graph().ainvoke(_input())
    assert out["node_reports"][-1].status == "SKIPPED"
    assert env.llm.seen == [] and env.status is None


# ── pure pieces: welcome guardrails, channel names, context window, tool schemas ────────────────────────

def test_welcome_guardrails():
    ok = "Welcome Jane to the onboarding! {{csm}} will share kickoff options here and walk you through every phase."
    assert check_welcome(ok, None) == []
    assert any("whole channel" in p for p in check_welcome(ok + " cc @here", None))
    assert any("raw Slack mention" in p for p in check_welcome(ok + " ping <@U123ABC>", None))
    assert any("too short" in p for p in check_welcome("Hi!", None))
    assert any("too long" in p for p in check_welcome("x" * 3000, None))
    assert any("evil.example" in p for p in check_welcome(ok + " see https://evil.example/login", "https://rl.example/p/1"))
    assert check_welcome(ok + " Track tasks at https://rl.example/p/1/tasks", "https://rl.example/p/1") == []
    assert any("not the onboarding project" in p for p in check_welcome(ok + " https://rl.example/p/1", None))


def test_welcome_placeholders_become_mentions_or_names():
    text = "Hi {{csm}} and {{ manager }} and {{ae}}"
    out = render_welcome(text, {"csm": "U1"}, {"manager": "Mia Manager", "ae": None})
    assert out == "Hi <@U1> and Mia Manager and your account executive"


def test_channel_names_must_be_slack_safe_and_carry_the_tier_prefix():
    assert validate_channel_name("#Nova-Ent-Acme", "nova-ent-") == "nova-ent-acme"
    for bad in ("nova-ent-acme corp", "nova-ent-Ünï", "", "other-acme"):
        with pytest.raises(OpError):
            validate_channel_name(bad, "nova-ent-")


def test_the_context_window_keeps_the_system_prompt_and_brief_and_never_starts_on_a_tool_result():
    msgs = [SystemMessage(content="sys"), HumanMessage(content="brief")]
    for i in range(30):
        msgs += [call("lookup_team_in_slack"), ToolMessage(content=f"r{i}", tool_call_id=f"t{i}")]
    windowed = window(msgs, keep=7)
    assert windowed[:2] == msgs[:2] and len(windowed) <= 2 + 7
    assert not isinstance(windowed[2], ToolMessage)


def test_injected_arguments_are_hidden_from_the_model():
    assert set(create_slack_channel.args) == {"name"}                       # state and tool_call_id are injected, not model-chosen
    assert set(post_welcome_message.args) == {"text"}
    assert set(invite_customer_to_channel.args) == set()                    # the model cannot pick the address to invite
    assert {t.name for t in TOOLS} == set(FULL_RUN)


def _provisioning_edges(monkeypatch, rocketlane_enabled: bool):
    from langgraph.checkpoint.memory import InMemorySaver

    from src.agents.provisioning import graph as provisioning_graph_module

    monkeypatch.setattr(provisioning_graph_module, "get_settings", lambda: SimpleNamespace(rocketlane_enabled=rocketlane_enabled))
    graph = provisioning_graph_module.build_provisioning_graph(InMemorySaver()).get_graph()
    return set(graph.nodes), {(e.source, e.target) for e in graph.edges}


def test_the_provisioning_graph_runs_the_slack_step_right_after_the_project(monkeypatch):
    nodes_, edges = _provisioning_edges(monkeypatch, rocketlane_enabled=True)
    assert "assign_team" not in nodes_
    assert ("__start__", "create_rocketlane_project") in edges
    assert ("create_rocketlane_project", "communication_agent") in edges
    assert ("communication_agent", "__end__") in edges


def test_with_rocketlane_disabled_provisioning_assigns_the_team_then_goes_straight_to_slack(monkeypatch):
    nodes_, edges = _provisioning_edges(monkeypatch, rocketlane_enabled=False)
    assert "create_rocketlane_project" not in nodes_
    assert ("__start__", "assign_team") in edges
    assert ("assign_team", "communication_agent") in edges
    assert ("communication_agent", "__end__") in edges


async def test_assign_team_records_the_csm_manager_and_dates_without_calling_rocketlane(monkeypatch):
    from datetime import timedelta

    from src.agents.provisioning import nodes as provisioning_nodes
    from src.services import CsTeamMemberService, PlanTierService

    saved = {}

    async def get_by_code(self, code):
        return SimpleNamespace(duration_days=30)

    async def owner_and_manager(self, pool):
        return SimpleNamespace(id="csm-1", email="csm@x.test"), SimpleNamespace(id="mgr-1", email="mgr@x.test")

    async def set_team_and_dates(self, onboarding_id, **kwargs):
        saved.update(kwargs, onboarding_id=onboarding_id)

    class NeverCalled:
        def __init__(self):
            raise AssertionError("Rocketlane must not be contacted when it is disabled")

    monkeypatch.setattr(PlanTierService, "get_by_code", get_by_code)
    monkeypatch.setattr(CsTeamMemberService, "get_owner_and_manager", owner_and_manager)
    monkeypatch.setattr(OnboardingService, "set_team_and_dates", set_team_and_dates)
    monkeypatch.setattr(provisioning_nodes, "RocketlaneClient", NeverCalled)

    assert await provisioning_nodes.assign_team_node({"onboarding_id": ONBOARDING_ID, "plan_tier": "growth"}) == {}
    assert (saved["csm_id"], saved["manager_id"]) == ("csm-1", "mgr-1")
    assert saved["target_go_live_date"] - saved["start_date"] == timedelta(days=30)


async def test_assign_team_without_a_configured_tier_stops_for_review(monkeypatch):
    from src.agents.provisioning import nodes as provisioning_nodes
    from src.services import PlanTierService

    status = {}

    async def no_tier(self, code):
        return None

    async def update_status(self, _id, new_status, reason=None):
        status["value"] = OnboardingStatus(new_status).value

    monkeypatch.setattr(PlanTierService, "get_by_code", no_tier)
    monkeypatch.setattr(OnboardingService, "update_status", update_status)

    with pytest.raises(provisioning_nodes.ProvisioningError):
        await provisioning_nodes.assign_team_node({"onboarding_id": ONBOARDING_ID, "plan_tier": "growth"})
    assert status["value"] == "needs_human_review"
