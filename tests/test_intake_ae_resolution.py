"""Which AE does a deal belong to? The LLM only sees the email body, which rarely names the AE's address —
the AE is the *sender*. These tests pin down that the real From address is used, so the tier workflow
always knows who to call and email."""
import os

os.environ.setdefault("GOOGLE_API_KEY", "test-key-not-used")

from types import SimpleNamespace

import pytest

from src.agents.intake import nodes
from src.agents.intake.models import DealEmailExtraction
from src.models.entities import AccountExecutive, Onboarding
from src.services import (
    AccountExecutiveService,
    AuditEventService,
    InboundEmailService,
    OnboardingService,
    WorkflowRunService,
)

RAJ = AccountExecutive(id="ae-raj", name="Raj Kumar", email="raj@novacrm.test", phone_e164="+14155550123")
SAM = AccountExecutive(id="ae-sam", name="Sam Seller", email="sam@novacrm.test", phone_e164="+14155550124")


def extraction(ae_email="", ae_name=""):
    return DealEmailExtraction(
        is_deal_closed=True, confidence="high", customer_name="Acme Corp", customer_contact_name="Jane Doe",
        customer_contact_email="jane@acme.test", ae_name=ae_name, ae_email=ae_email,
        sf_opportunity_id="006X", deal_summary="Acme signed.", reasoning="Closed-won.",
    )


@pytest.fixture
def run_extract(monkeypatch):
    """Runs ``extract_deal_node`` with the LLM and the database replaced; returns (state update, created row args)."""
    async def go(extracted, sender, known_aes=(RAJ, SAM)):
        created = {}

        class Chain:
            async def ainvoke(self, _payload):
                return extracted

        async def get_by_email(self, address):
            return next((ae for ae in known_aes if ae.email.lower() == address.lower()), None)

        async def create(self, **kwargs):
            created.update(kwargs)
            return Onboarding(id=kwargs["onboarding_id"], inbound_email_id="e1", customer_name="Acme Corp", customer_contact_email="j@a.co")

        async def noop(self, *args, **kwargs):
            return None

        monkeypatch.setattr(nodes, "_get_deal_parser_chain", lambda: Chain())
        monkeypatch.setattr(AccountExecutiveService, "get_by_email", get_by_email)
        monkeypatch.setattr(OnboardingService, "create", create)
        monkeypatch.setattr(InboundEmailService, "update_classification", noop)
        monkeypatch.setattr(WorkflowRunService, "create", noop)
        monkeypatch.setattr(AuditEventService, "log_event", noop)
        state = {"inbound_email_id": "e1", "sender": sender, "subject": "Closed: Acme", "body": "We closed Acme!"}
        return await nodes.extract_deal_node(state), created

    return go


async def test_the_sender_is_the_ae_when_the_body_does_not_name_one(run_extract):
    """The bug: the LLM returned no AE address, so the workflow never learned who the AE was."""
    update, created = await run_extract(extraction(ae_email="", ae_name=""), "Raj Kumar <raj@novacrm.test>")

    assert (update["ae_email"], update["ae_name"], update["ae_id"]) == ("raj@novacrm.test", "Raj Kumar", "ae-raj")
    assert created["ae_id"] == "ae-raj"
    assert created["extra_details"]["ae_email"] == "raj@novacrm.test"          # what the tier workflow is started from


async def test_an_ae_the_model_found_in_the_body_is_preferred_when_known(run_extract):
    """A forwarded email: the sender isn't the AE, but the body names one we know."""
    update, _ = await run_extract(extraction(ae_email="sam@novacrm.test"), "Assistant <assistant@novacrm.test>")
    assert (update["ae_email"], update["ae_id"], update["ae_name"]) == ("sam@novacrm.test", "ae-sam", "Sam Seller")


async def test_a_model_address_we_do_not_know_falls_back_to_the_known_sender(run_extract):
    update, _ = await run_extract(extraction(ae_email="hallucinated@nowhere.test"), "Raj Kumar <raj@novacrm.test>")
    assert (update["ae_email"], update["ae_id"]) == ("raj@novacrm.test", "ae-raj")


async def test_an_unknown_ae_still_gets_an_address_to_email(run_extract):
    """No account_executives row at all (so no phone): the escalation email still needs someone to go to."""
    update, created = await run_extract(extraction(), "Priya Seller <priya@elsewhere.test>", known_aes=())
    assert (update["ae_email"], update["ae_name"], update["ae_id"]) == ("priya@elsewhere.test", "Priya Seller", None)
    assert created["ae_id"] is None and created["extra_details"] == {"ae_name": "Priya Seller", "ae_email": "priya@elsewhere.test"}


async def test_the_models_values_are_used_when_the_sender_is_not_an_ae_either(run_extract):
    update, _ = await run_extract(extraction(ae_email="boss@acme-sales.test", ae_name="Bo Boss"), "Assistant <assistant@x.test>", known_aes=())
    assert (update["ae_email"], update["ae_name"]) == ("boss@acme-sales.test", "Bo Boss")


async def test_the_address_match_ignores_case_and_display_names(run_extract):
    update, _ = await run_extract(extraction(), '"Kumar, Raj" <RAJ@NovaCRM.test>')
    assert update["ae_id"] == "ae-raj"
