import operator
from typing import Annotated, Literal

from langgraph.graph.message import add_messages

from src.state.base_state import IdentityState, NodeReportState


class CommunicationState(IdentityState, NodeReportState, total=False):
    """What the communication agent takes from, and gives back to, its parent graph.
    ``ProvisioningState`` inherits it, so every key here flows back to the parent."""

    # ── Inputs (from the parent graph) ──────────────────────────────
    plan_tier: Literal["enterprise", "growth"] | None
    rocketlane_project_id: str | None
    customer_contact_name: str
    customer_contact_email: str

    # ── Channel ────────────────────────────────────────────────────────
    slack_channel_id: str | None
    slack_channel_name: str
    slack_channel_topic: str
    slack_welcome_text: str
    slack_internal_user_ids: list[str]
    slack_invite_status: Literal["sent", "failed"] | None
    """Slack Connect invite to the customer's contact."""
    slack_warnings: Annotated[list[str], operator.add]
    """Non-fatal problems; any warning sends the onboarding to human review."""


class CommunicationAgentState(CommunicationState, total=False):
    """The agent's own working state. It lives only inside the communication subgraph (the subgraph's
    input and output schemas are ``CommunicationState``), so the provisioning checkpoint stays small."""

    messages: Annotated[list, add_messages]
    """The ReAct conversation: system prompt, the facts brief, the LLM's tool calls and the tool results."""
    steps: int
    """LLM turns used so far (bounded by ``communication_agent_max_steps``)."""
    agent_failed: bool
    """True when the LLM could not be used; the standard flow then completes the setup."""
    slack_mentions: dict[str, str]
    """role (csm / manager / ae) → Slack member ID, filled by the team-lookup tool."""
    slack_steps_tried: Annotated[list[str], operator.add]
    """Steps a tool has already attempted (channel, team, topic, welcome, customer); the finalizer only
    completes the ones that were never attempted."""
