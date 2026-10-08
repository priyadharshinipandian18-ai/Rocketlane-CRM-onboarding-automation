"""Checkpoints must keep restoring with the types we store, even when LangGraph starts refusing
unregistered ones (its warning says a future version will)."""
import logging
import os

os.environ.setdefault("GOOGLE_API_KEY", "test-key-not-used")

from src.agents.commons.guardrail.models import GuardrailCheck
from src.agents.commons.models import NodeReport
from src.config.checkpointer_config import ALLOWED_MSGPACK_MODULES, build_serde


def test_node_reports_round_trip_without_an_unregistered_type_warning(caplog):
    report = NodeReport(producer="confirm_tier", status="RESOLVED", reasoning="Tier 'growth' confirmed.")
    serde = build_serde()
    with caplog.at_level(logging.WARNING):
        restored = serde.loads_typed(serde.dumps_typed({"node_reports": [report]}))

    assert restored["node_reports"] == [report] and isinstance(restored["node_reports"][0], NodeReport)
    assert not [r for r in caplog.records if "unregistered" in r.getMessage()]


def test_the_guardrail_verdict_round_trips_too(caplog):
    check = GuardrailCheck(checked=True, blocked=True, rule="phishing_detector", category="phishing",
                           findings=["phishing:credential_request"], reasoning="Phishing signals matched")
    serde = build_serde()
    with caplog.at_level(logging.WARNING):
        restored = serde.loads_typed(serde.dumps_typed({"guardrail_check": check}))

    assert restored["guardrail_check"] == check and isinstance(restored["guardrail_check"], GuardrailCheck)
    assert not [r for r in caplog.records if "unregistered" in r.getMessage()]


def test_the_stored_types_are_registered():
    assert ("src.agents.commons.models", "NodeReport") in ALLOWED_MSGPACK_MODULES
    assert ("src.agents.commons.guardrail.models", "GuardrailCheck") in ALLOWED_MSGPACK_MODULES
