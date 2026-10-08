import functools
import logging
import re
import uuid
from email.utils import parseaddr

from langchain_core.prompts import ChatPromptTemplate

from src.agents.commons.models import NodeReport
from src.agents.intake.models import DealEmailExtraction
from src.agents.intake.state import IntakeState
from src.config.model_factory import get_deal_parser_llm
from src.config.settings import get_settings
from src.models.entities import AccountExecutive
from src.models.enums import EmailKind, EmailKindSource, InboundEmailStatus, WorkflowName, WorkflowRunStatus
from src.prompts import load_prompt
from src.services import (
    AccountExecutiveService,
    AuditEventService,
    InboundEmailService,
    OnboardingService,
    WorkflowRunService,
)

log = logging.getLogger(__name__)

@functools.cache
def _get_deal_parser_chain():
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", load_prompt("intake/deal_parser_system")),
            ("human", load_prompt("intake/deal_parser_human")),
        ]
    )
    return prompt | get_deal_parser_llm().with_structured_output(DealEmailExtraction)


async def _invoke_with_retries(chain, payload, attempts: int | None = None):
    attempts = attempts or get_settings().llm_attempts
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await chain.ainvoke(payload)
        except Exception as exc:
            last_exc = exc
            log.warning("LLM call failed (attempt %d/%d): %s", attempt, attempts, exc)
    raise last_exc


async def resolve_ae(extraction: DealEmailExtraction, sender: str) -> tuple[AccountExecutive | None, str, str]:
    """Who is the AE for this deal? Returns ``(AE row or None, email, name)``.

    The LLM only knows what the email *body* says, and a deal email usually doesn't spell out the
    AE's address — the AE is the person who **sent** it. So: prefer an AE we know from the address
    the model found, then from the actual ``From`` header; if neither is in ``app.account_executives``
    fall back to the extracted values, then the sender, so the tier workflow always has an address
    to email (an AE without a phone row still gets the escalation).
    """
    display_name, sender_address = parseaddr(sender)
    accounts = AccountExecutiveService()
    for candidate in (extraction.ae_email, sender_address):
        if candidate:
            ae = await accounts.get_by_email(candidate)
            if ae is not None:
                return ae, ae.email, ae.name
    return None, extraction.ae_email or sender_address, extraction.ae_name or display_name


def same_customer(a: str | None, b: str | None) -> bool:
    """Do two customer names refer to the same company? Case, spacing, punctuation and a trailing
    legal suffix are ignored (``Acme Corp.`` == ``ACME corporation``)."""
    def key(name: str | None) -> str:
        words = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower()).split()
        while words and words[-1] in {"corp", "corporation", "inc", "incorporated", "llc", "ltd", "limited", "co", "company", "gmbh", "plc"}:
            words.pop()
        return " ".join(words)
    return bool(key(a)) and key(a) == key(b)


def route_after_classification(state: IntakeState) -> str:
    return "extract_deal" if state.get("kind") == EmailKind.DEAL_CLOSED.value else "__end__"


async def extract_deal_node(state: IntakeState) -> dict:
    """The classifier says this is a closed deal: extract the customer / AE details, create the
    ``onboardings`` row, and queue the tier-confirmation workflow as a ``pending`` run.

    The ``onboarding_id`` is generated here. The AE's name and email are kept on the onboarding
    (``extra_details``) so the tier workflow can still email an AE who isn't in
    ``app.account_executives``.
    """
    inbound_email_id = state["inbound_email_id"]
    inbound_emails, onboardings = InboundEmailService(), OnboardingService()

    extraction: DealEmailExtraction = await _invoke_with_retries(
        _get_deal_parser_chain(),
        {"sender": state["sender"], "subject": state["subject"], "body": state["body"]},
    )

    if not extraction.is_deal_closed:
        log.info("Email classified as NOT a closed deal | subject=%s", state["subject"])
        await inbound_emails.update_classification(
            inbound_email_id, is_deal_email=False, classification=extraction.model_dump(),
            status=InboundEmailStatus.IGNORED, kind=EmailKind.NO_DEAL, kind_source=EmailKindSource.LLM,
        )
        return {
            "is_deal_closed": False,
            "kind": EmailKind.NO_DEAL.value,
            "node_reports": [NodeReport(producer="extract_deal", status="NOT_A_DEAL", reasoning=extraction.reasoning)],
        }

    ae, ae_email, ae_name = await resolve_ae(extraction, state["sender"])
    onboarding_id = str(uuid.uuid4())
    extra_details = {"ae_name": ae_name, "ae_email": ae_email}
    if extraction.sf_opportunity_url:
        extra_details["sf_opportunity_url"] = extraction.sf_opportunity_url

    sf_opportunity_id = extraction.sf_opportunity_id or None

    async def create(sf_id: str | None):
        return await onboardings.create(
            onboarding_id=onboarding_id,
            inbound_email_id=inbound_email_id,
            customer_name=extraction.customer_name,
            customer_contact_email=extraction.customer_contact_email,
            customer_contact_name=extraction.customer_contact_name or None,
            ae_id=ae.id if ae else None,
            sf_opportunity_id=sf_id,
            extra_details=extra_details,
        )

    onboarding = await create(sf_opportunity_id)
    if onboarding is None:
        # The opportunity ID is already onboarded. Same customer → the AE re-sent the same deal. A
        # DIFFERENT customer under the same ID is not a re-send (Salesforce IDs are unique per deal): it is
        # a typo, a template or a placeholder — dropping it would silently lose a real deal.
        existing = await onboardings.find_one(sf_opportunity_id=sf_opportunity_id)
        if existing is None or same_customer(existing.customer_name, extraction.customer_name):
            log.info("Duplicate deal (opportunity %s already onboarded for %s) | customer=%s",
                     sf_opportunity_id, existing.customer_name if existing else "?", extraction.customer_name)
            await inbound_emails.update_classification(
                inbound_email_id, is_deal_email=False, classification={**extraction.model_dump(), "duplicate": True},
                status=InboundEmailStatus.IGNORED, kind=EmailKind.DEAL_CLOSED, kind_source=EmailKindSource.LLM,
            )
            return {
                "is_deal_closed": False,
                "node_reports": [
                    NodeReport(
                        producer="extract_deal", status="DUPLICATE",
                        reasoning=f"Opportunity '{sf_opportunity_id}' already has an onboarding for the same customer.",
                    )
                ],
            }
        log.warning(
            "Opportunity %s is already used by '%s' but this deal is for '%s' — onboarding it anyway WITHOUT that "
            "opportunity ID (check the ID in the email)", sf_opportunity_id, existing.customer_name, extraction.customer_name,
        )
        extra_details["sf_opportunity_id_conflict"] = {"id": sf_opportunity_id, "already_used_by": existing.customer_name}
        sf_opportunity_id = None
        onboarding = await create(None)

    await inbound_emails.update_classification(
        inbound_email_id, is_deal_email=True, classification=extraction.model_dump(),
        status=InboundEmailStatus.PARSED, kind=EmailKind.DEAL_CLOSED, kind_source=EmailKindSource.LLM,
    )
    await WorkflowRunService().create(
        WorkflowName.TIER_CONFIRMATION, f"tier:{onboarding_id}", onboarding_id=onboarding_id, status=WorkflowRunStatus.PENDING
    )
    await AuditEventService().log_event(onboarding_id, actor="intake_agent", event_type="email_parsed", payload=extraction.model_dump())

    log.info("Email classified as a closed deal | customer=%s ae=%s <%s>", extraction.customer_name, ae_name, ae_email)
    return {
        "is_deal_closed": True,
        "onboarding_id": onboarding_id,
        "customer_name": extraction.customer_name,
        "customer_contact_name": extraction.customer_contact_name,
        "customer_contact_email": extraction.customer_contact_email,
        "ae_name": ae_name,
        "ae_email": ae_email,
        "ae_id": ae.id if ae else None,
        "sf_opportunity_id": sf_opportunity_id,
        "deal_summary": extraction.deal_summary,
        "node_reports": [NodeReport(producer="extract_deal", status="SUCCESS", reasoning=extraction.reasoning)],
    }
