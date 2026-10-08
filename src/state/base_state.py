"""Reusable ``TypedDict`` building blocks for the agents' state classes.

Each agent's state (``OnboardingState``, ``EmailAnalyserState``,
``CommunicationState``) extends the pieces it needs. Because a subgraph's
results only flow back to its parent for keys the parent also declares, the
parent state *inherits* the subgraph's state — one definition per key, so the
two can never drift apart.
"""
from typing import Annotated, TypedDict

from src.agents.commons.models import NodeReport
from src.state.reducers import append_node_reports


class IdentityState(TypedDict, total=False):
    """The ids a run is keyed by (``inbound_email_id`` for intake, ``onboarding_id`` afterwards)."""

    onboarding_id: str
    inbound_email_id: str


class RawEmailState(TypedDict, total=False):
    """The email, already persisted to ``app.inbound_emails`` before the graph runs."""

    sender: str
    subject: str
    body: str


class NodeReportState(TypedDict, total=False):
    """The reasoning trail every node appends to."""

    node_reports: Annotated[list[NodeReport], append_node_reports]
