"""Duplicate deals: the same Salesforce opportunity for the SAME customer is a re-send and is ignored; the
same ID under a DIFFERENT customer is a data problem and must not silently drop a real deal."""
import logging
import os

os.environ.setdefault("GOOGLE_API_KEY", "test-key-not-used")

from types import SimpleNamespace

import pytest

from src.agents.intake import nodes
from src.agents.intake.models import DealEmailExtraction
from src.models.entities import AccountExecutive, Onboarding
from src.models.enums import InboundEmailStatus
from src.services import (
    AccountExecutiveService,
    AuditEventService,
    InboundEmailService,
    OnboardingService,
    WorkflowRunService,
)

SF_ID = "006Ab000001XyZtAAK"


def extraction(customer, sf_id=SF_ID):
    return DealEmailExtraction(
        is_deal_closed=True, confidence="high", customer_name=customer, customer_contact_name="Jane",
        customer_contact_email="jane@x.test", ae_name="Raj", ae_email="raj@novacrm.test",
        sf_opportunity_id=sf_id, deal_summary="signed", reasoning="closed",
    )


@pytest.fixture
def deals(monkeypatch):
    """An in-memory stand-in for ``onboardings`` with the UNIQUE (sf_opportunity_id) rule."""
    store, created, classified, runs = {}, [], [], []

    async def create(self, **kwargs):
        sf = kwargs["sf_opportunity_id"]
        if sf is not None and sf in store:
            return None                                              # ON CONFLICT DO NOTHING
        row = Onboarding(id=kwargs["onboarding_id"], inbound_email_id="e", customer_name=kwargs["customer_name"],
                         customer_contact_email="j@x.test", sf_opportunity_id=sf, extra_details=kwargs["extra_details"])
        if sf is not None:
            store[sf] = row
        created.append(row)
        return row

    async def find_one(self, **filters):
        return store.get(filters["sf_opportunity_id"])

    async def get_by_email(self, address):
        return AccountExecutive(id="ae", name="Raj", email=address, phone_e164="+1")

    async def update_classification(self, email_id, is_deal_email, classification, status, **kw):
        classified.append((is_deal_email, classification, InboundEmailStatus(status).value, kw))

    async def noop(self, *a, **k):
        return None

    async def run_create(self, workflow, thread_id, **kw):
        runs.append(thread_id)

    monkeypatch.setattr(OnboardingService, "create", create)
    monkeypatch.setattr(OnboardingService, "find_one", find_one)
    monkeypatch.setattr(AccountExecutiveService, "get_by_email", get_by_email)
    monkeypatch.setattr(InboundEmailService, "update_classification", update_classification)
    monkeypatch.setattr(AuditEventService, "log_event", noop)
    monkeypatch.setattr(WorkflowRunService, "create", run_create)

    async def run(customer, sf_id=SF_ID):
        class Chain:
            async def ainvoke(self, _payload):
                return extraction(customer, sf_id)

        monkeypatch.setattr(nodes, "_get_deal_parser_chain", lambda: Chain())
        return await nodes.extract_deal_node(
            {"inbound_email_id": "e1", "sender": "Raj <raj@novacrm.test>", "subject": "Deal closed", "body": "x"}
        )

    return SimpleNamespace(run=run, created=created, classified=classified, runs=runs)


async def test_a_new_opportunity_is_onboarded_normally(deals):
    update = await deals.run("Teena Corporation")
    assert update["is_deal_closed"] is True and update["sf_opportunity_id"] == SF_ID
    assert len(deals.created) == 1 and len(deals.runs) == 1


@pytest.mark.parametrize("again", ["Teena Corporation", "teena corporation", "Teena Corp.", "TEENA  CORP", "Teena"])
async def test_the_same_opportunity_for_the_same_customer_is_a_resend_and_is_ignored(deals, again):
    await deals.run("Teena Corporation")
    update = await deals.run(again)

    assert update["is_deal_closed"] is False and update["node_reports"][0].status == "DUPLICATE"
    assert len(deals.created) == 1 and len(deals.runs) == 1                     # nothing new started
    assert deals.classified[-1][1]["duplicate"] is True and deals.classified[-1][2] == "ignored"


async def test_the_same_opportunity_id_for_a_different_customer_is_still_onboarded(deals, caplog):
    """The reported case: only the customer name changed between three test emails."""
    await deals.run("Teena Corporation")
    with caplog.at_level(logging.WARNING):
        update = await deals.run("Jalebi Corporation")

    assert update["is_deal_closed"] is True and update["sf_opportunity_id"] is None      # onboarded, without the conflicting ID
    assert [o.customer_name for o in deals.created] == ["Teena Corporation", "Jalebi Corporation"]
    assert len(deals.runs) == 2                                                    # its tier workflow was queued
    assert deals.created[1].extra_details["sf_opportunity_id_conflict"] == {"id": SF_ID, "already_used_by": "Teena Corporation"}
    assert any("already used by 'Teena Corporation'" in r.getMessage() for r in caplog.records)


async def test_a_third_customer_with_the_same_id_is_onboarded_too(deals):
    for customer in ("Teena Corporation", "Gadona Corporation", "Jalebi Corporation"):
        assert (await deals.run(customer))["is_deal_closed"] is True
    assert len(deals.created) == 3


async def test_deals_without_an_opportunity_id_are_never_duplicates(deals):
    assert (await deals.run("Acme", sf_id=""))["is_deal_closed"] is True
    assert (await deals.run("Acme", sf_id=""))["is_deal_closed"] is True            # NULL ids don't collide
    assert len(deals.created) == 2


@pytest.mark.parametrize("a, b, expected", [
    ("Acme Corp.", "ACME corporation", True), ("Acme", "Acme Inc", True), ("Teena Corporation", "Jalebi Corporation", False),
    ("Acme Ltd", "Acme Labs", False), ("", "", False), (None, "Acme", False),
])
def test_same_customer_comparison(a, b, expected):
    assert nodes.same_customer(a, b) is expected
