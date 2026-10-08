"""Reading the plan from a call transcript when Bolna returns no extractions.

The rule is as strict as the agent extraction it stands in for: the AE stated one plan, the assistant read
exactly that plan back, and the AE's very next turn affirmed it. Everything else is "unknown"."""
import pytest

from src.integrations.voice_ai_client import TRANSCRIPT_CONFIDENCE, decide_outcome, parse_transcript

# The transcript of a real call (the one that came back "unclear" because Bolna sent no extractions).
REAL_CALL = (
    "assistant: Hi testuser, this is Nova, an automated assistant from NovaCRM Customer Success. This call may be recorded. "
    "I'm calling about the Teena Corporation deal you just closed. Do you have thirty seconds?\n"
    "user:  yes\n"
    "assistant:  Thanks for that. Could you tell me, is Teena Corporation on the Enterprise plan or the Growth plan?\n"
    "user:  growth\n"
    "assistant:   Just to confirm, Teena Corporation is on the Growth plan, correct?\n"
    "user:  yes\n"
    "assistant: Thank you, that is all I needed. Teena Corporation is confirmed on the Growth plan. Have a great day.\n"
)


def call(*turns):
    return "\n".join(f"{who}: {text}" for who, text in turns)


def test_the_real_call_is_confirmed_growth():
    parsed = parse_transcript(REAL_CALL)
    assert parsed["plan_tier"] == "growth" and parsed["readback_confirmed"] is True
    assert parsed["confidence"] == TRANSCRIPT_CONFIDENCE and "transcript" in parsed["raw_plan_value"]


def test_the_result_is_accepted_by_the_normal_outcome_rules():
    parsed = parse_transcript(REAL_CALL)
    outcome = decide_outcome("completed", parsed["plan_tier"], parsed["readback_confirmed"], parsed["confidence"], 0.7,
                             hangup_reason="end_call_tool")
    assert outcome["outcome"] == "confirmed" and outcome["plan_tier"] == "growth"


def test_a_stricter_confidence_setting_switches_the_transcript_path_off():
    parsed = parse_transcript(REAL_CALL)
    outcome = decide_outcome("completed", parsed["plan_tier"], parsed["readback_confirmed"], parsed["confidence"], 0.9)
    assert outcome["outcome"] != "confirmed"


def test_enterprise_works_too():
    assert parse_transcript(call(("assistant", "Which plan?"), ("user", "Enterprise."),
                                 ("assistant", "So that's Enterprise, right?"), ("user", "Yeah that's right")))["plan_tier"] == "enterprise"


@pytest.mark.parametrize("transcript", [
    None, "", "no turns here at all",
    # a voicemail: only the assistant speaks
    call(("assistant", "Hi, this is Nova calling about Acme. Is it Enterprise or Growth? Please call back.")),
    # the AE never states a plan
    call(("assistant", "Which plan?"), ("user", "hmm not sure, let me check"), ("assistant", "No problem, goodbye.")),
    # the AE states one, but the assistant reads back the OTHER one and the AE says yes to it
    call(("assistant", "Which plan?"), ("user", "enterprise"), ("assistant", "So it's Growth, correct?"), ("user", "yes")),
    # no read-back at all
    call(("assistant", "Which plan?"), ("user", "growth"), ("assistant", "Thanks, bye.")),
    # the AE refuses the read-back
    call(("assistant", "Which plan?"), ("user", "growth"), ("assistant", "Just to confirm, Growth?"), ("user", "no, not growth")),
    # the AE answers with a negation or both words
    call(("assistant", "Which plan?"), ("user", "not growth"), ("assistant", "Growth?"), ("user", "yes")),
    call(("assistant", "Which plan?"), ("user", "enterprise or growth, I don't remember"), ("assistant", "Growth?"), ("user", "yes")),
    # the read-back is a question naming both plans
    call(("assistant", "Enterprise or Growth?"), ("user", "growth"), ("assistant", "Enterprise or Growth, right?"), ("user", "yes")),
    # the confirmation is hedged
    call(("assistant", "Which plan?"), ("user", "growth"), ("assistant", "Growth, correct?"), ("user", "yes, well, actually no")),
    # the assistant's closing words are not a confirmation by themselves
    call(("assistant", "Great, Acme is confirmed on the Growth plan. Bye.")),
])
def test_anything_unclear_is_unknown(transcript):
    parsed = parse_transcript(transcript)
    assert parsed["plan_tier"] == "unknown" and parsed["readback_confirmed"] is False and parsed["confidence"] is None


def test_a_correction_after_a_confirmation_cancels_it():
    assert parse_transcript(call(
        ("assistant", "Which plan?"), ("user", "growth"), ("assistant", "Growth, correct?"), ("user", "yes"),
        ("assistant", "Anything else?"), ("user", "oh wait, it's enterprise"),
    ))["plan_tier"] == "unknown"


def test_the_ae_can_correct_themselves_before_the_readback():
    assert parse_transcript(call(
        ("assistant", "Which plan?"), ("user", "growth"), ("assistant", "Growth, correct?"), ("user", "no"),
        ("assistant", "Which plan then?"), ("user", "enterprise"), ("assistant", "Enterprise, correct?"), ("user", "yes"),
    ))["plan_tier"] == "enterprise"


def test_continuation_lines_and_casing_are_handled():
    transcript = "Assistant: Which plan\nis it?\nUSER: GROWTH\nassistant: Growth, correct?\nuser: YES"
    assert parse_transcript(transcript)["plan_tier"] == "growth"
