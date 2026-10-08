import os

os.environ.setdefault("GOOGLE_API_KEY", "test-key-not-used")

from src.integrations.voice_ai_client import decide_outcome, is_terminal, map_call_status, parse_extraction

MIN = 0.7


def test_parse_current_grouped_format():
    data = {"Plan confirmation": {
        "plan_tier": {"objective": "enterprise", "confidence": 0.93},
        "readback_confirmed": {"objective": "yes", "confidence": 0.9},
    }}
    assert parse_extraction(data) == {
        "plan_tier": "enterprise", "readback_confirmed": True, "confidence": 0.93, "raw_plan_value": "enterprise",
    }


def test_parse_legacy_flat_format_and_key_spelling():
    data = {"Plan Tier": "Growth plan", "Readback Confirmed": "Yes"}
    result = parse_extraction(data)
    assert result["plan_tier"] == "growth" and result["readback_confirmed"] is True
    assert result["confidence"] is None


def test_parse_json_string_and_missing_data():
    assert parse_extraction('{"plan_tier": "enterprise", "readback_confirmed": "no"}')["readback_confirmed"] is False
    assert parse_extraction(None)["plan_tier"] == "unknown"
    assert parse_extraction("not json")["plan_tier"] == "unknown"


def test_ambiguous_plan_is_unknown():
    assert parse_extraction({"plan_tier": "enterprise or growth, not sure"})["plan_tier"] == "unknown"
    assert parse_extraction({"plan_tier": {"objective": "unknown"}})["plan_tier"] == "unknown"


def test_confirmed_only_with_readback_and_confidence():
    assert decide_outcome("completed", "growth", True, 0.9, MIN) == {"outcome": "confirmed", "plan_tier": "growth"}
    assert decide_outcome("completed", "growth", True, None, MIN)["outcome"] == "confirmed"
    assert decide_outcome("completed", "growth", False, 0.9, MIN) == {"outcome": "unclear"}
    assert decide_outcome("completed", "growth", True, 0.4, MIN) == {"outcome": "unclear"}
    assert decide_outcome("completed", "unknown", True, 0.9, MIN) == {"outcome": "unclear"}


def test_unanswered_and_failed_calls():
    assert decide_outcome("no-answer", None, False, None, MIN)["outcome"] == "no_answer"
    assert decide_outcome("busy", None, False, None, MIN)["outcome"] == "no_answer"
    assert decide_outcome("failed", None, False, None, MIN)["outcome"] == "failed"
    assert decide_outcome("balance-low", None, False, None, MIN)["outcome"] == "failed"
    assert decide_outcome("completed", "unknown", False, None, MIN, hangup_reason="voicemail_detected")["outcome"] == "voicemail"


def test_status_helpers():
    assert is_terminal("completed") and is_terminal("no-answer") and is_terminal("ERROR")
    assert not is_terminal("call-disconnected") and not is_terminal("in-progress")
    assert map_call_status("in-progress") == "in_progress"
    assert map_call_status("initiated") == "ringing"
    assert map_call_status("no-answer") == "no_answer"
    assert map_call_status("weird") is None
