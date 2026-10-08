"""Structural tests of the three workflow graphs — no live Gmail/Gemini/Bolna/Rocketlane calls.

``test_graphs_compile_with_postgres_checkpointer`` additionally needs a reachable Postgres
(``POSTGRES_URI``) with ``db/schema.sql`` applied, since the checkpointer's ``setup()`` issues real
DDL. It skips itself with a clear message if no Postgres is reachable.
"""
import os

os.environ.setdefault("GOOGLE_API_KEY", "test-key-not-used")

import pytest

from src.agents.intake.state import IntakeState
from src.agents.provisioning.state import ProvisioningState
from src.agents.tier_confirmation.state import TierConfirmationState

INTAKE_NODES = ["email_analyser", "extract_deal"]
TIER_NODES = [
    "place_plan_tier_call", "await_plan_tier_result", "schedule_call_retry", "wait_for_call_retry",
    "request_ae_reply", "await_ae_reply", "confirm_tier",
]
PROVISIONING_NODES = ["create_rocketlane_project", "communication_agent"]


def test_intake_state_shape():
    assert set(IntakeState.__annotations__) == {
        "inbound_email_id", "onboarding_id", "sender", "subject", "body", "gmail_thread_id",
        "kind", "kind_source", "escalation_id", "skip_reason",
        "raw_email_text", "safe_email_text", "guardrail_check", "security_status", "security_findings",
        "is_deal_closed", "confidence_score", "closing_evidence", "analysis_reasoning", "analysis_failed",
        "validated_status",
        "customer_name", "customer_contact_name", "customer_contact_email",
        "ae_name", "ae_email", "ae_id", "sf_opportunity_id", "deal_summary",
        "node_reports",
    }


def test_tier_state_shape():
    assert set(TierConfirmationState.__annotations__) == {
        "onboarding_id", "inbound_email_id", "node_reports",
        "customer_name", "customer_contact_name", "customer_contact_email", "ae_name", "ae_email", "ae_id",
        "call_attempts", "call_placement", "voice_call_id", "call_outcome", "next_call_at",
        "plan_tier", "plan_tier_source",
    }


def test_provisioning_state_shape():
    assert set(ProvisioningState.__annotations__) == {
        "onboarding_id", "inbound_email_id", "node_reports",
        "plan_tier", "rocketlane_project_id", "customer_name", "customer_contact_name", "customer_contact_email",
        "slack_channel_id", "slack_channel_name", "slack_channel_topic", "slack_welcome_text",
        "slack_internal_user_ids", "slack_invite_status", "slack_warnings",
    }


def test_the_checkpoints_never_hold_the_raw_email_or_other_bulky_state():
    """The tier and provisioning states are deliberately small: no email text at all."""
    for state in (TierConfirmationState, ProvisioningState):
        assert not {"body", "raw_email_text", "safe_email_text", "sender", "subject"} & set(state.__annotations__)


def test_graphs_compile_in_memory_with_their_nodes():
    from langgraph.checkpoint.memory import InMemorySaver

    from src.agents.intake.graph import build_intake_graph
    from src.agents.provisioning.graph import build_provisioning_graph
    from src.agents.tier_confirmation.graph import build_tier_confirmation_graph

    for build, expected in (
        (build_intake_graph, INTAKE_NODES),
        (build_tier_confirmation_graph, TIER_NODES),
        (build_provisioning_graph, PROVISIONING_NODES),
    ):
        nodes = build(InMemorySaver()).get_graph().nodes
        for name in expected:
            assert name in nodes


def test_the_classifier_has_reply_detection_before_the_llm():
    from src.agents.email_analyser.graph import build_email_analyser_graph

    graph = build_email_analyser_graph().get_graph()
    for name in ("guardrail", "detect_escalation_reply", "handle_escalation_reply", "analyze_deal_status"):
        assert name in graph.nodes
    for folded_in in ("pii_redactor", "sender_gate", "injection_detector", "phishing_detector"):
        assert folded_in not in graph.nodes                                 # one guardrail node now, not four
    edges = {(e.source, e.target) for e in graph.edges}
    # the single guardrail node comes first and routes on its verdict …
    assert ("fetch_email", "guardrail") in edges
    assert ("guardrail", "archive_skip") in edges            # blocked: not an AE
    assert ("guardrail", "log_and_reject") in edges          # blocked: injection / phishing
    assert ("guardrail", "detect_escalation_reply") in edges  # passed
    # … and only then reply detection, then the deal-closed analysis
    assert ("detect_escalation_reply", "handle_escalation_reply") in edges
    assert ("detect_escalation_reply", "analyze_deal_status") in edges
    assert ("fetch_email", "detect_escalation_reply") not in edges


async def test_graphs_compile_with_postgres_checkpointer():
    from src.agents.intake.graph import intake_graph
    from src.agents.provisioning.graph import provisioning_graph
    from src.agents.tier_confirmation.graph import tier_graph

    try:
        compiled = [await g.compiled() for g in (intake_graph, tier_graph, provisioning_graph)]
    except Exception as exc:
        pytest.skip(f"No reachable Postgres for the checkpointer ({exc!r}) — set POSTGRES_URI and apply db/schema.sql to run this test")
        return

    for graph, expected in zip(compiled, (INTAKE_NODES, TIER_NODES, PROVISIONING_NODES)):
        for name in expected:
            assert name in graph.get_graph().nodes
