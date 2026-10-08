"""Reading an email reply to "plan tier needed" — pure functions, no I/O.

The rule that matters: a reply picks a tier only when it names exactly one of
ENTERPRISE / GROWTH, without negating it, in the part the person wrote.
"""
import pytest

from src.agents.tier_confirmation.tier_reply import (
    clarification_email,
    extract_reply_text,
    extract_token,
    make_token,
    parse_tier,
    tier_request_email,
)

OID = "3fe0c709-8f82-4af2-95b8-ff938d18b214"


def test_token_is_short_stable_and_found_in_a_reply_subject():
    token = make_token(OID)
    assert token == "ONB-3fe0c709"
    assert extract_token(f"Re: [{token}] Plan tier needed: Acme") == token
    assert extract_token("RE: [onb-3FE0C709] anything") == token          # case-insensitive, normalised
    assert extract_token("Closed: Acme Corp") is None
    assert extract_token(None) is None
    assert extract_token("[ONB-xyz] not hex") is None


@pytest.mark.parametrize("reply, expected", [
    ("Enterprise", "enterprise"),
    ("GROWTH", "growth"),
    ("  growth.  Thanks!", "growth"),
    ("Hi team, it's Enterprise — they signed the 3-year deal", "enterprise"),
    ("Enterprise\n\nOn Wed, Oct 7, 2026 at 12:10 PM Bot <bot@x.co> wrote:\n> reply with ENTERPRISE or GROWTH", "enterprise"),
    ("Growth\n> Please reply with ENTERPRISE or GROWTH", "growth"),
    ("Enterprise On Wed, Oct 7, 2026 at 12:10 PM Bot <bot@x.co> wrote: Hi Raj, ... ENTERPRISE or GROWTH", "enterprise"),
])
def test_a_clear_answer_is_accepted_and_the_quoted_original_is_ignored(reply, expected):
    assert parse_tier(reply) == expected


@pytest.mark.parametrize("reply", [
    "Enterprise or Growth?",              # both
    "not sure yet",                        # neither
    "It's not Enterprise",                 # negated
    "Not growth, enterprise",              # negation anywhere makes it unclear
    "I don't know, maybe growth",
    "",
    None,
    "Let me check with finance",
    "> ENTERPRISE\n> GROWTH",              # only quoted text
])
def test_anything_unclear_is_not_a_tier(reply):
    assert parse_tier(reply) is None


def test_extract_reply_text_drops_quotes_and_headers():
    text = "Growth please\n\n-----Original Message-----\nFrom: Bot\nSubject: x\nENTERPRISE or GROWTH"
    assert extract_reply_text(text) == "Growth please"
    assert extract_reply_text("only\n> quoted") == "only"
    assert extract_reply_text(None) == ""


def test_request_email_carries_the_token_and_the_instruction():
    subject, body = tier_request_email("ONB-3fe0c709", "Raj", "Acme Corp", "Plan tier not confirmed after 3 call attempt(s).")
    assert subject == "[ONB-3fe0c709] Plan tier needed: Acme Corp"
    assert "Hi Raj," in body and "ENTERPRISE" in body and "GROWTH" in body and "[ONB-3fe0c709]" in body
    assert "3 call attempt(s))" in body                                   # the trailing period is tidied


def test_clarification_email_and_subjects_we_send_are_recognised_on_the_way_back():
    assert "one word" in clarification_email("ONB-3fe0c709", "Acme Corp")[0].lower()
    request_subject = tier_request_email("ONB-3fe0c709", "Raj", "Acme", "no answer")[0]
    for subj in (request_subject, clarification_email("ONB-3fe0c709", "Acme")[0]):
        assert extract_token("Re: " + subj) == "ONB-3fe0c709"
