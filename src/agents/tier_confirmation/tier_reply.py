"""Asking a human for the plan tier by email, and reading the answer.

Pure functions — no I/O. The tier is decided *only* by a strict parser: exactly
one of the words ENTERPRISE / GROWTH, no negation, in the part of the reply the
person actually wrote (not the quoted original). Anything else is "unclear" and
gets a "please answer with one word" email — a stray or ambiguous reply can
never pick a tier.
"""
import re
from typing import Literal

Tier = Literal["enterprise", "growth"]

_TOKEN = re.compile(r"\[ONB-([0-9a-f]{8})\]", re.IGNORECASE)
_TIER_WORD = re.compile(r"\b(enterprise|growth)\b", re.IGNORECASE)
_NEGATION = re.compile(r"\b(not|isn'?t|aren'?t|wasn'?t|never|except|neither|instead of)\b|n't\b", re.IGNORECASE)
_QUOTE_START = re.compile(
    r"^\s*>"                                   # "> quoted line"
    r"|^\s*On .{0,120}?wrote:?\s*$"            # "On Tue, 6 Oct 2026, Priya <p@x> wrote:"
    r"|^\s*-{2,}\s*Original Message\s*-{2,}"   # Outlook
    r"|^\s*From:\s.+",                         # forwarded / Outlook header block
    re.IGNORECASE,
)
# Gmail flattens "On … wrote:" into the same line as the reply in snippets.
_INLINE_WROTE = re.compile(r"\s+On .{0,120}?wrote:.*$", re.IGNORECASE | re.DOTALL)


def make_token(onboarding_id: str) -> str:
    """The token that rides in the subject so a reply finds its onboarding: ``ONB-ab12cd34``."""
    return "ONB-" + onboarding_id.replace("-", "")[:8].lower()


def extract_token(subject: str | None) -> str | None:
    match = _TOKEN.search(subject or "")
    return f"ONB-{match.group(1).lower()}" if match else None


def extract_reply_text(text: str | None) -> str:
    """What the sender wrote, without the quoted message below it."""
    kept: list[str] = []
    for line in (text or "").splitlines():
        if _QUOTE_START.match(line):
            break
        kept.append(line)
    return _INLINE_WROTE.sub("", "\n".join(kept)).strip()


def parse_tier(text: str | None) -> Tier | None:
    """``enterprise`` / ``growth`` only when the reply names exactly one of them
    and doesn't negate it; otherwise ``None`` (unclear)."""
    reply = extract_reply_text(text)
    words = {w.lower() for w in _TIER_WORD.findall(reply)}
    if len(words) != 1 or _NEGATION.search(reply):
        return None
    return words.pop()  # type: ignore[return-value]


def tier_request_email(token: str, ae_name: str, customer_name: str, reason: str) -> tuple[str, str]:
    subject = f"[{token}] Plan tier needed: {customer_name}"
    body = (
        f"Hi {ae_name or 'there'},\n\n"
        f"We couldn't confirm the plan tier for {customer_name} by phone ({reason.rstrip('.')}).\n\n"
        "Please reply to this email with ONE word:\n\n"
        "    ENTERPRISE\n"
        "    or\n"
        "    GROWTH\n\n"
        "As soon as we have it, onboarding continues automatically. "
        "Please keep the [" + token + "] tag in the subject.\n\n"
        "Thanks,\nCustomer Onboarding Automation\n"
    )
    return subject, body


def clarification_email(token: str, customer_name: str) -> tuple[str, str]:
    subject = f"[{token}] Please answer with one word: {customer_name}"
    body = (
        "Thanks for the reply — I couldn't tell which plan you meant.\n\n"
        "Please reply with exactly ONE word: ENTERPRISE or GROWTH.\n\n"
        "Thanks,\nCustomer Onboarding Automation\n"
    )
    return subject, body
