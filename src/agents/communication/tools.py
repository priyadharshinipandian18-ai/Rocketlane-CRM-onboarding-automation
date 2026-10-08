"""The agent's tools. Each is a thin, validated wrapper around one ``ChannelOps`` operation.

* The model passes only what it must (a channel name, a topic, the welcome text). The onboarding it acts
  on comes from the graph state (``InjectedState``), the people and addresses come from the database; the
  model can never aim a tool at another onboarding or another email address.
* A tool never raises to the model: success and failure both come back as a short, actionable
  ``ToolMessage`` ("ERROR (temporary…)" / "ERROR (permanent…)"), plus state updates (``Command``).
* Slack calls inside are retried on temporary failures (``operations.with_retry``); what reaches the model
  has already been retried.
"""
import logging
from typing import Annotated

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command

from src.agents.communication.operations import ChannelOps, OpError
from src.agents.communication.welcome import check_welcome, render_welcome

log = logging.getLogger(__name__)

State = Annotated[dict, InjectedState]
CallId = Annotated[str, InjectedToolCallId]


def _reply(call_id: str, text: str, error: bool = False, **update) -> Command:
    return Command(update={**update, "messages": [ToolMessage(content=text, tool_call_id=call_id, status="error" if error else "success")]})


def _failure(call_id: str, step: str, exc: OpError, tried: bool = True, warning: str | None = None, **update) -> Command:
    kind = "temporary, already retried" if exc.retryable else f"permanent: {exc.code}"
    text = f"ERROR ({kind}) {exc}"
    if exc.code == "name_taken":
        text = f"ERROR (name_taken) {exc}. Call create_slack_channel again with the second name in channel_name_options."
    elif not exc.retryable:
        text += " Do not retry this tool; continue with the remaining steps."
    if tried:
        update["slack_steps_tried"] = [step]
    if warning:
        update["slack_warnings"] = [warning]
    log.warning("Tool %s failed for the Slack agent: %s", step, exc)
    return _reply(call_id, text, error=True, **update)


@tool
async def lookup_team_in_slack(state: State, tool_call_id: CallId) -> Command:
    """Find the CSM, CS manager and account executive in the Slack workspace (by their stored Slack ID or work
    email). Call this first. Returns which roles were found; the welcome message needs it to mention them."""
    ops = ChannelOps(state["onboarding_id"])
    try:
        team = await ops.resolve_team()
    except OpError as exc:
        return _failure(tool_call_id, "team_lookup", exc, tried=False)
    found = ", ".join(sorted(team.user_ids)) or "nobody"
    return _reply(
        tool_call_id, f"Found in Slack: {found}. Roles not listed could not be matched and will be named in plain text.",
        slack_mentions=team.user_ids, slack_warnings=team.warnings, slack_steps_tried=["lookup"],
    )


@tool
async def create_slack_channel(name: str, state: State, tool_call_id: CallId) -> Command:
    """Create the shared customer channel (or reuse the one that already exists). `name` must be one of the
    channel_name_options from the facts: lowercase letters, digits, - and _ only."""
    ops = ChannelOps(state["onboarding_id"])
    try:
        result = await ops.create_channel(name)
    except OpError as exc:
        return _failure(
            tool_call_id, "channel", exc, tried=exc.code != "name_taken",
            warning=None if exc.code == "name_taken" else f"Could not create the Slack channel: {exc.code}",
        )
    note = "Channel already existed; reusing it." if result.reused else "Channel created."
    return _reply(
        tool_call_id, f"{note} name={result.name}",
        slack_channel_id=result.channel_id, slack_channel_name=result.name, slack_steps_tried=["channel"],
    )


@tool
async def set_channel_topic(topic: str, state: State, tool_call_id: CallId) -> Command:
    """Set the channel topic (max 250 characters): plan, customer, timeline and who the CSM is."""
    ops = ChannelOps(state["onboarding_id"])
    try:
        await ops.set_topic(topic)
    except OpError as exc:
        return _failure(tool_call_id, "topic", exc, warning=f"Could not set the channel topic: {exc.code}")
    return _reply(tool_call_id, "Topic set.", slack_channel_topic=topic[:250], slack_steps_tried=["topic"])


@tool
async def invite_internal_team(state: State, tool_call_id: CallId) -> Command:
    """Add the CSM, CS manager and AE (the ones found by lookup_team_in_slack) to the channel."""
    ops = ChannelOps(state["onboarding_id"])
    ids = list((state.get("slack_mentions") or {}).values())
    try:
        count = await ops.invite_team(ids)
    except OpError as exc:
        return _failure(tool_call_id, "team", exc, warning=f"Could not add the internal team: {exc.code}")
    text = f"Invited {count} teammate(s)." if count else "Nobody to invite (no team member was found in Slack)."
    return _reply(tool_call_id, text, slack_internal_user_ids=ids, slack_steps_tried=["team"])


@tool
async def post_welcome_message(text: str, state: State, tool_call_id: CallId) -> Command:
    """Post the welcome message to the channel (only once). Write it as described in your instructions and
    mention teammates only as {{csm}}, {{manager}}, {{ae}}. If it breaks a rule, the tool says what to change."""
    ops = ChannelOps(state["onboarding_id"])
    ctx = await ops.context()
    if ctx is None:
        return _failure(tool_call_id, "welcome", OpError("no_context", "no confirmed plan tier for this onboarding"))
    problems = check_welcome(text, ctx.project_url)
    if problems:
        return _reply(
            tool_call_id, "REJECTED, not posted. Fix and call post_welcome_message again: " + "; ".join(problems) + ".", error=True
        )
    names = {"csm": ctx.csm_name, "manager": ctx.manager_name, "ae": ctx.ae_name}
    rendered = render_welcome(text, state.get("slack_mentions") or {}, names)
    try:
        outcome = await ops.post_welcome(rendered)
    except OpError as exc:
        return _failure(tool_call_id, "welcome", exc, warning=f"Could not post the welcome message: {exc.code}")
    msg = "Welcome message was already posted earlier; nothing sent." if outcome == "already_posted" else "Welcome message posted."
    return _reply(tool_call_id, msg, slack_welcome_text=rendered, slack_steps_tried=["welcome"])


@tool
async def invite_customer_to_channel(state: State, tool_call_id: CallId) -> Command:
    """Send the customer's contact a Slack Connect invite to the channel. The address is taken from the
    onboarding record; do not pass one. If it fails, do not retry: the CSM is notified in the channel."""
    ops = ChannelOps(state["onboarding_id"])
    try:
        result = await ops.invite_customer()
    except OpError as exc:
        return _failure(tool_call_id, "customer", exc, warning=f"Slack Connect invite failed: {exc.code}")
    if result.status == "failed":
        return _reply(
            tool_call_id, f"ERROR (permanent: {result.error}) The invite failed and the CSM was notified in the channel. Do not retry.",
            error=True, slack_invite_status="failed", slack_steps_tried=["customer"],
            slack_warnings=[f"Slack Connect invite to the customer failed: {result.error}"],
        )
    note = "Invite had already been sent." if result.already else "Slack Connect invite sent."
    return _reply(tool_call_id, note, slack_invite_status="sent", slack_steps_tried=["customer"])


TOOLS = [
    lookup_team_in_slack,
    create_slack_channel,
    set_channel_topic,
    invite_internal_team,
    post_welcome_message,
    invite_customer_to_channel,
]
