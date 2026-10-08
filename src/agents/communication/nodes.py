"""Nodes of the communication agent (a ReAct loop around the Slack tools).

    prepare ──▶ agent ⇄ tools ──▶ finalize

* ``prepare``  builds the model's context (system prompt + a small facts brief).
* ``agent``    asks the tool-calling LLM for the next step; if the LLM can't be used it says so and moves on.
* ``tools``    runs the tool calls (``ToolNode``); tool errors come back to the model as messages.
* ``finalize`` is the safety net and the bookkeeper: it **completes whatever the agent did not attempt** with
  the same operations (deterministically, the standard welcome template standing in for the LLM's), then
  decides the outcome from what is actually true in the database, never from what the model claims.
"""
import logging

from langchain_core.messages import HumanMessage, SystemMessage

from src.agents.communication.channel_plan import build_channel_plan
from src.agents.communication.operations import ChannelOps, OpError
from src.agents.communication.context import build_brief, system_prompt, window
from src.agents.communication.state import CommunicationAgentState
from src.agents.communication.tools import TOOLS
from src.agents.commons.models import NodeReport
from src.config.model_factory import get_agent_llm
from src.config.settings import get_settings
from src.models.enums import OnboardingStatus
from src.services import AuditEventService, OnboardingService, SlackChannelService

log = logging.getLogger(__name__)


def route_enabled(state: CommunicationAgentState) -> str:
    return "prepare" if get_settings().slack_bot_token else "skip_communication"


def skip_communication_node(state: CommunicationAgentState) -> dict:
    log.warning("SLACK_BOT_TOKEN not set — skipping shared-channel creation for onboarding %s", state.get("onboarding_id"))
    return {
        "node_reports": [
            NodeReport(producer="communication_agent", status="SKIPPED", reasoning="SLACK_BOT_TOKEN is not configured.")
        ]
    }


# ── prepare ───────────────────────────────────────────────────────────────

async def prepare_node(state: CommunicationAgentState) -> dict:
    """Load the facts and start the model's conversation (only once; a rerun keeps the existing one)."""
    onboarding_id = state["onboarding_id"]
    ctx = await ChannelOps(onboarding_id).context()
    if ctx is None:
        return {
            "agent_failed": True,
            "slack_warnings": [f"Onboarding {onboarding_id} has no confirmed plan tier to personalise the channel from."],
        }
    if state.get("messages"):
        return {}
    plan = build_channel_plan(ctx, onboarding_id)
    return {"messages": [SystemMessage(content=system_prompt()), HumanMessage(content=build_brief(ctx, plan))], "steps": 0}


def route_after_prepare(state: CommunicationAgentState) -> str:
    return "finalize" if state.get("agent_failed") else "agent"


# ── agent ─────────────────────────────────────────────────────────────────

def _tool_llm():
    return get_agent_llm().bind_tools(TOOLS)


async def agent_node(state: CommunicationAgentState) -> dict:
    """One model turn. A model failure is retried (``LLM_ATTEMPTS``); if it persists the agent steps aside and
    ``finalize`` completes the setup without it."""
    settings = get_settings()
    steps = state.get("steps", 0) + 1
    last_exc: Exception | None = None
    for attempt in range(1, max(1, settings.llm_attempts) + 1):
        try:
            reply = await _tool_llm().ainvoke(window(state["messages"]))
            return {"messages": [reply], "steps": steps}
        except Exception as exc:
            last_exc = exc
            log.warning("Slack agent LLM call failed (attempt %d/%d): %r", attempt, settings.llm_attempts, exc)
    log.error("Slack agent LLM unavailable after %d attempts — the standard flow will finish the setup: %r",
              settings.llm_attempts, last_exc)
    return {"agent_failed": True, "steps": steps}


def route_after_agent(state: CommunicationAgentState) -> str:
    if state.get("agent_failed"):
        return "finalize"
    last = state["messages"][-1]
    if getattr(last, "tool_calls", None):
        if state.get("steps", 0) >= get_settings().communication_agent_max_steps:
            log.warning("Slack agent hit its step limit (%d) — finishing with the standard flow", state.get("steps", 0))
            return "finalize"
        return "tools"
    return "finalize"


def tool_error_message(exc: Exception) -> str:
    """Anything a tool raises (bad arguments, an unexpected bug) goes back to the model as text."""
    log.warning("Slack agent tool raised %r", exc)
    return f"ERROR (permanent: {type(exc).__name__}) {exc}. Check the arguments; if it keeps failing, skip this step."


# ── finalize ──────────────────────────────────────────────────────────────

async def finalize_node(state: CommunicationAgentState) -> dict:
    onboarding_id = state["onboarding_id"]
    ops, channels = ChannelOps(onboarding_id), SlackChannelService()
    tried = set(state.get("slack_steps_tried") or [])
    new_warnings: list[str] = []

    ctx = await ops.context()
    if ctx is None:
        return await _failed(state, state.get("slack_warnings") or [])

    plan = build_channel_plan(ctx, onboarding_id)
    row = await channels.get_by_onboarding(onboarding_id)
    channel_id = row.slack_channel_id if row else None

    # 1. the channel itself
    if not channel_id and "channel" not in tried:
        try:
            channel_id = (await ops.create_channel_with_fallback(plan.name_candidates)).channel_id
        except OpError as exc:
            new_warnings.append(f"Could not create the Slack channel: {exc.code}")
    if not channel_id:
        return await _failed(state, (state.get("slack_warnings") or []) + new_warnings)

    # 2. whatever the agent did not attempt
    mentions = dict(state.get("slack_mentions") or {})
    if "lookup" not in tried:
        team = await ops.resolve_team()
        mentions = dict(team.user_ids)
        new_warnings += team.warnings
        plan = build_channel_plan(ctx, onboarding_id)           # the template welcome can now mention them
    if "team" not in tried:
        try:
            await ops.invite_team(list(mentions.values()))
        except OpError as exc:
            new_warnings.append(f"Could not add the internal team: {exc.code}")
    if "topic" not in tried:
        try:
            await ops.set_topic(plan.topic)
        except OpError as exc:
            new_warnings.append(f"Could not set the channel topic: {exc.code}")
    if "welcome" not in tried:
        try:
            await ops.post_welcome(plan.welcome_text)
        except OpError as exc:
            new_warnings.append(f"Could not post the welcome message: {exc.code}")
    if "customer" not in tried:
        try:
            invite = await ops.invite_customer()
            if invite.status == "failed":
                new_warnings.append(f"Slack Connect invite to the customer failed: {invite.error}")
        except OpError as exc:
            new_warnings.append(f"Slack Connect invite failed: {exc.code}")

    row = await channels.get_by_onboarding(onboarding_id)
    warnings = (state.get("slack_warnings") or []) + new_warnings
    channel_name = (row.channel_name if row else None) or state.get("slack_channel_name")
    invite_status = row.invite_status if row and row.invite_status in ("sent", "failed") else None
    note = " (standard flow: the LLM was unavailable)" if state.get("agent_failed") else ""

    payload = {"slack_channel_id": channel_id, "channel_name": channel_name, "invite_status": invite_status, "warnings": warnings}
    onboardings, audit = OnboardingService(), AuditEventService()
    if warnings:
        reason = "Slack channel set up with problems: " + "; ".join(warnings)
        await onboardings.update_status(onboarding_id, OnboardingStatus.NEEDS_HUMAN_REVIEW, reason)
        await audit.log_event(onboarding_id, actor="communication_agent", event_type="slack_channel_partial", payload=payload)
        log.warning("%s onboarding=%s", reason, onboarding_id)
        report = NodeReport(producer="communication_agent", status="NEEDS_HUMAN", reasoning=reason + note)
    else:
        await onboardings.update_status(onboarding_id, OnboardingStatus.CHANNEL_CREATED)
        await audit.log_event(onboarding_id, actor="communication_agent", event_type="slack_channel_created", payload=payload)
        report = NodeReport(
            producer="communication_agent", status="CHANNEL_CREATED",
            reasoning=f"Shared channel #{channel_name} created and the customer invited.{note}",
        )
    return {
        "slack_channel_id": channel_id,
        "slack_channel_name": channel_name,
        "slack_invite_status": invite_status,
        "slack_warnings": new_warnings,
        "node_reports": [report],
    }


async def _failed(state: CommunicationAgentState, warnings: list[str]) -> dict:
    """No channel exists. The Rocketlane project already does, so this does NOT fail the onboarding: it flags it
    for a human."""
    onboarding_id = state["onboarding_id"]
    reason = "Slack channel was not created: " + ("; ".join(warnings) or "unknown error")
    await SlackChannelService().mark_failed(onboarding_id, reason)
    await OnboardingService().update_status(onboarding_id, OnboardingStatus.NEEDS_HUMAN_REVIEW, reason)
    await AuditEventService().log_event(onboarding_id, actor="communication_agent", event_type="slack_channel_failed", payload={"reason": reason})
    log.error("%s onboarding=%s", reason, onboarding_id)
    return {"slack_channel_id": None, "node_reports": [NodeReport(producer="communication_agent", status="FAILED", reasoning=reason)]}
