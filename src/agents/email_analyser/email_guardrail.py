"""The email analyser's guardrail: ONE node that runs, in order,

    redact PII  →  sender gate  →  prompt-injection check  →  phishing check

and routes on the result (``route_after_guardrail``):

    not an AE      → archive_skip     (no_deal, rule)
    injection      → log_and_reject   (malicious, rule)
    phishing       → log_and_reject   (malicious, rule)
    all clear      → detect_escalation_reply  (classification starts)

Cheap, deterministic rules only: no LLM sees an email before it has passed. The rules themselves are pure
functions in ``guardrails.py``; this module wires them into the reusable node in ``agents/commons/guardrail``.
"""
import logging
from email.utils import parseaddr

from src.agents.commons.guardrail import GuardrailRule, create_guardrail_node, route_after_guardrail
from src.agents.email_analyser import guardrails
from src.agents.email_analyser.state import EmailAnalyserState
from src.config.settings import get_settings
from src.services import AccountExecutiveService

log = logging.getLogger(__name__)

NOT_AN_AE = "not_an_ae"
PROMPT_INJECTION = "prompt_injection"
PHISHING = "phishing"


def _redact_pii(state: EmailAnalyserState) -> dict:
    """Not a block: produce the text every later rule (and the LLM) works on."""
    safe, counts = guardrails.redact_pii(state["raw_email_text"])
    if counts:
        log.info("Redacted PII from email | %s", counts)
    return {"safe_email_text": safe}


async def _sender_is_not_an_ae(state: EmailAnalyserState) -> list[str]:
    """Deal emails come from AEs. Anything else (newsletters, recruiters, system notices) is stopped here, before any
    LLM call is spent on it. Turned off with ``REQUIRE_AE_SENDER=false``."""
    if not get_settings().require_ae_sender:
        return []
    address = parseaddr(state.get("sender", ""))[1]
    if address and await AccountExecutiveService().get_by_email(address):
        return []
    log.info("Sender %r is not an active account executive — not a deal email", address)
    return ["sender_not_an_account_executive"]


def _prompt_injection(state: EmailAnalyserState) -> list[str]:
    return guardrails.detect_injection(state["safe_email_text"])


def _phishing(state: EmailAnalyserState) -> list[str]:
    return [f"phishing:{name}" for name in guardrails.detect_phishing(state["safe_email_text"], state.get("sender", ""))]


def _blocked_as_malicious(findings: list[str], state: dict) -> dict:
    return {"security_status": "malicious", "security_findings": findings}


def _blocked_as_not_an_ae(findings: list[str], state: dict) -> dict:
    address = parseaddr(state.get("sender", ""))[1]
    return {"skip_reason": findings[0], "analysis_reasoning": f"Sender {address!r} is not an active AE."}


EMAIL_GUARDRAIL_RULES = [
    GuardrailRule(
        name="sender_gate", category=NOT_AN_AE, check=_sender_is_not_an_ae,
        reason="Sender is not an account executive", report_status="NOT_A_DEAL", on_block=_blocked_as_not_an_ae,
    ),
    GuardrailRule(
        name="injection_detector", category=PROMPT_INJECTION, check=_prompt_injection,
        reason="Prompt-injection patterns matched", on_block=_blocked_as_malicious,
    ),
    GuardrailRule(
        name="phishing_detector", category=PHISHING, check=_phishing,
        reason="Phishing signals matched", on_block=_blocked_as_malicious,
    ),
]


def create_email_guardrail_node():
    return create_guardrail_node(
        EMAIL_GUARDRAIL_RULES,
        prepare=_redact_pii,
        on_pass={"security_status": "safe", "security_findings": []},
        producer="guardrail",
    )


# Where a blocked email goes, by category. The first entry is also the fallback for a category nobody mapped.
GUARDRAIL_ROUTES = {PROMPT_INJECTION: "log_and_reject", PHISHING: "log_and_reject", NOT_AN_AE: "archive_skip"}


def route_after_email_guardrail(state: EmailAnalyserState) -> str:
    return route_after_guardrail(state, GUARDRAIL_ROUTES, passed="detect_escalation_reply")
