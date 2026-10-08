from typing import Literal

from src.agents.commons.guardrail.models import GuardrailCheck
from src.state.base_state import IdentityState, NodeReportState, RawEmailState


class EmailAnalyserState(IdentityState, RawEmailState, NodeReportState, total=False):
    """State of the email-analyser subgraph (the classifier). ``IntakeState`` inherits it,
    so every key here flows back to the intake workflow automatically.

    ``body`` / ``raw_email_text`` are already PII-redacted by the time they get here.
    """

    # ── Input ────────────────────────────────────────────────────────────
    gmail_thread_id: str | None
    """Gmail thread of the email — how a reply is matched to the escalation we sent."""

    # ── The classification ─────────────────────────────────────────────
    kind: Literal["deal_closed", "no_deal", "escalation_reply", "malicious"]
    """The outcome. Unset when the analysis itself failed (the email is stored as failed)."""
    kind_source: Literal["thread_match", "token", "llm", "rule"]
    escalation_id: str | None
    """Set when ``kind == "escalation_reply"``: the escalation this answers."""
    skip_reason: str | None
    """A rule that short-circuited the LLM (e.g. the sender isn't an AE)."""

    # ── Pipeline ───────────────────────────────────────────────────────
    raw_email_text: str
    """The incoming email as one string (redacted body)."""
    safe_email_text: str
    """``raw_email_text`` after the guardrail's PII redaction."""
    guardrail_check: GuardrailCheck
    """The guardrail's verdict (blocked or not, which rule, what category); the graph routes on it."""
    security_status: Literal["safe", "malicious"]
    security_findings: list[str]
    """Which prompt-injection / phishing rules matched (empty when safe)."""
    is_deal_closed: bool
    """The LLM's verdict — not trusted until ``validated_status`` confirms it."""
    confidence_score: float
    """0.0–1.0, the LLM's own certainty."""
    closing_evidence: list[str]
    """Verbatim quotes the LLM says prove the deal closed."""
    analysis_reasoning: str
    analysis_failed: bool
    """True when the LLM call itself failed (distinct from "not a deal")."""
    validated_status: Literal["confirmed", "false_alarm", "failed_validation"]
