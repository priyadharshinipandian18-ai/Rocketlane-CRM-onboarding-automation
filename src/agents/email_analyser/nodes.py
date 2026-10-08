import functools
import logging

from langchain_core.prompts import ChatPromptTemplate

from src.agents.commons.models import NodeReport
from src.agents.email_analyser import guardrails
from src.agents.email_analyser.models import DealStatusAnalysis
from src.agents.email_analyser.state import EmailAnalyserState
from src.config.model_factory import get_deal_parser_llm
from src.config.settings import get_settings
from src.models.enums import EmailKind, EmailKindSource, InboundEmailStatus
from src.prompts import load_prompt
from src.services import EscalationService, InboundEmailService

log = logging.getLogger(__name__)

MIN_CONFIDENCE = 0.7
"""A closed-deal verdict below this confidence is rejected as a false alarm."""

@functools.cache
def _get_analyser_chain():
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", load_prompt("email_analyser/deal_status_system")),
            ("human", load_prompt("email_analyser/deal_status_human")),
        ]
    )
    chain = prompt | get_deal_parser_llm().with_structured_output(DealStatusAnalysis)
    return chain.with_retry(stop_after_attempt=get_settings().llm_attempts)


# ── fetch_email ────────────────────────────────────────────────────────────

def fetch_email_node(state: EmailAnalyserState) -> dict:
    """Assemble ``raw_email_text``.

    The mail fetch itself already happened upstream: Gmail's watch() → Pub/Sub →
    ``GmailIntakeService`` fetches each new message, redacts it and hands it to the
    graph, so this node only joins the pieces into one string. (The body in the state is
    already PII-redacted — the raw body never reaches the checkpointer.)
    """
    raw = f"From: {state.get('sender', '')}\nSubject: {state.get('subject', '')}\n\n{state.get('body', '')}"
    return {"raw_email_text": raw}


# ── detect_escalation_reply (deterministic) ────────────────────────────────

async def detect_escalation_reply_node(state: EmailAnalyserState) -> dict:
    """Is this email a reply to an escalation we sent the AE? Decided by evidence, never by
    an LLM: the Gmail thread of the email *we* sent, or the ``[ONB-…]`` token in the subject.
    Whether the sender may answer, and what they said, is decided later (``TierReplyService``).
    """
    match = await EscalationService().match_reply(state.get("gmail_thread_id"), state.get("subject"))
    if match is None:
        return {}
    escalation, source = match
    log.info("Email is a reply to escalation %s (onboarding %s) via %s", escalation.id, escalation.onboarding_id, source.value)
    return {"kind": EmailKind.ESCALATION_REPLY.value, "kind_source": source.value, "escalation_id": escalation.id}


def route_after_reply_detection(state: EmailAnalyserState) -> str:
    return "handle_escalation_reply" if state.get("kind") == EmailKind.ESCALATION_REPLY.value else "analyze_deal_status"


async def handle_escalation_reply_node(state: EmailAnalyserState) -> dict:
    """Record that this is an escalation reply. Acting on it (authorising the sender, reading
    the answer, resuming the tier workflow) is the orchestrator's follow-up — this graph only
    classifies."""
    await InboundEmailService().update_classification(
        state["inbound_email_id"],
        is_deal_email=False,
        classification={"escalation_reply": True},
        status=InboundEmailStatus.PARSED,
        kind=EmailKind.ESCALATION_REPLY,
        kind_source=state.get("kind_source"),
        escalation_id=state.get("escalation_id"),
    )
    return {
        "is_deal_closed": False,
        "node_reports": [
            NodeReport(producer="handle_escalation_reply", status="SUCCESS", reasoning="Reply to an escalation email; passed on to the tier workflow.")
        ],
    }


# ── analyze_deal_status (LLM) ──────────────────────────────────────────────

async def analyze_deal_status_node(state: EmailAnalyserState) -> dict:
    """Ask the LLM whether the (redacted) email reports a closed deal. An LLM
    failure is recorded as ``analysis_failed`` rather than raised, so the
    validator can mark the email ``failed_validation`` instead of losing it.
    """
    try:
        analysis: DealStatusAnalysis = await _get_analyser_chain().ainvoke({"email": state["safe_email_text"]})
    except Exception as exc:
        log.exception("Deal-status analysis failed after %d attempts", get_settings().llm_attempts)
        return {
            "analysis_failed": True,
            "is_deal_closed": False,
            "confidence_score": 0.0,
            "closing_evidence": [],
            "analysis_reasoning": f"LLM analysis failed: {exc!r}",
        }

    return {
        "analysis_failed": False,
        "is_deal_closed": analysis.is_deal_closed,
        "confidence_score": min(max(analysis.confidence_score, 0.0), 1.0),
        "closing_evidence": analysis.closing_evidence,
        "analysis_reasoning": analysis.reasoning,
    }


# ── fact_check_validator (guardrail) ───────────────────────────────────────

def fact_check_validator_node(state: EmailAnalyserState) -> dict:
    """Check the LLM's "deal closed" claim against hard criteria. A closed
    verdict is only ``confirmed`` when ALL of these hold:

    * confidence ≥ ``MIN_CONFIDENCE``;
    * at least one of its evidence quotes appears verbatim in the email
      (catches hallucinated evidence);
    * the email uses a definitive closing phrase, or mentions a signed
      attachment (not just casual talk);
    * no tentative / negotiating language ("expected to close", "pending
      signature", …) anywhere in it.
    """
    if state.get("analysis_failed"):
        return _verdict("failed_validation", state.get("analysis_reasoning", "Analysis did not complete."), "FAILED")

    if not state.get("is_deal_closed"):
        return _verdict("false_alarm", state.get("analysis_reasoning", "Analyser judged this not a closed deal."), "NOT_A_DEAL")

    text = state["safe_email_text"]
    problems: list[str] = []

    confidence = state.get("confidence_score", 0.0)
    if confidence < MIN_CONFIDENCE:
        problems.append(f"confidence {confidence:.2f} is below {MIN_CONFIDENCE}")

    if not any(guardrails.quote_is_grounded(q, text) for q in state.get("closing_evidence", [])):
        problems.append("none of the cited evidence appears verbatim in the email")

    if not (guardrails.has_definitive_close(text) or guardrails.has_signed_attachment_mention(text)):
        problems.append("no definitive closing phrase or signed-attachment mention")

    hedges = guardrails.find_hedging(text)
    if hedges:
        problems.append(f"tentative language present ({'; '.join(sorted(set(h.lower() for h in hedges)))})")

    if problems:
        return _verdict("false_alarm", "Closed-deal claim failed validation: " + "; ".join(problems), "NOT_A_DEAL")
    return _verdict("confirmed", "Closed-deal claim passed every validation check.", "SUCCESS")


def _verdict(validated_status: str, reasoning: str, report_status: str) -> dict:
    return {
        "validated_status": validated_status,
        "node_reports": [NodeReport(producer="fact_check_validator", status=report_status, reasoning=reasoning)],
    }


def route_deal_validation(state: EmailAnalyserState) -> str:
    return "trigger_sales_system" if state.get("validated_status") == "confirmed" else "archive_skip"


# ── terminal nodes ─────────────────────────────────────────────────────────

def trigger_sales_system_node(state: EmailAnalyserState) -> dict:
    """The deal is confirmed. The "trigger" is the intake workflow carrying on to
    ``extract_deal``, which creates the onboarding and queues the tier workflow; nothing is
    written here. This is the place to add a CRM / Slack notification if wanted.
    """
    log.info("Deal confirmed by email analyser | subject=%s", state.get("subject"))
    return {
        "kind": EmailKind.DEAL_CLOSED.value,
        "kind_source": EmailKindSource.LLM.value,
        "node_reports": [
            NodeReport(producer="trigger_sales_system", status="SUCCESS", reasoning="Confirmed closed deal — handing off to deal parsing.")
        ]
    }


async def log_and_reject_node(state: EmailAnalyserState) -> dict:
    findings = state.get("security_findings", [])
    # ERROR level so log-based alerting can notify the administrator.
    log.error(
        "SECURITY: rejected email as malicious (prompt injection / phishing) | sender=%s subject=%s rules=%s",
        state.get("sender"), state.get("subject"), findings,
    )
    await InboundEmailService().update_classification(
        state["inbound_email_id"],
        is_deal_email=False,
        classification={"security_status": "malicious", "security_findings": findings},
        status=InboundEmailStatus.IGNORED,
        kind=EmailKind.MALICIOUS,
        kind_source=EmailKindSource.RULE,
    )
    return {
        "is_deal_closed": False,
        "kind": EmailKind.MALICIOUS.value,
        "kind_source": EmailKindSource.RULE.value,
        "node_reports": [
            NodeReport(producer="log_and_reject", status="SECURITY_REJECTED", reasoning="Email rejected and logged; no further processing.")
        ],
    }


async def archive_skip_node(state: EmailAnalyserState) -> dict:
    """Safely drop an email that is not a (validated) closed deal. A
    ``failed_validation`` email is stored as ``failed`` so it can be
    re-examined; everything else is ``ignored``.
    """
    failed = state.get("validated_status") == "failed_validation"
    skipped = state.get("skip_reason")
    # A decided "no deal" gets a kind; a failed analysis stays unclassified so it can be re-examined.
    kind = None if failed else EmailKind.NO_DEAL
    source = None if failed else (EmailKindSource.RULE if skipped else EmailKindSource.LLM)
    await InboundEmailService().update_classification(
        state["inbound_email_id"],
        is_deal_email=False,
        classification={
            "skipped": skipped,
            "validated_status": state.get("validated_status"),
            "llm_is_deal_closed": state.get("is_deal_closed"),
            "confidence_score": state.get("confidence_score"),
            "reasoning": state.get("analysis_reasoning"),
        },
        status=InboundEmailStatus.FAILED if failed else InboundEmailStatus.IGNORED,
        kind=kind,
        kind_source=source,
    )
    log.info("Email archived (%s) | subject=%s", skipped or state.get("validated_status"), state.get("subject"))
    update = {
        "is_deal_closed": False,
        "node_reports": [
            NodeReport(
                producer="archive_skip",
                status="FAILED" if failed else "NOT_A_DEAL",
                reasoning="Analysis could not complete; stored as failed." if failed else "Not a closed deal; archived.",
            )
        ],
    }
    if kind:
        update.update(kind=kind.value, kind_source=source.value)
    return update
