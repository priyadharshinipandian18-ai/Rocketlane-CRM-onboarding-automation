"""The HTTP layer: routers, auth dependencies and the error envelope.

Uses the real app over an ASGI transport, so route registration, exception
handlers and the response DTOs are all exercised. The tests that read
onboardings use a throwaway Postgres (``temp_database``).
"""
import os
from uuid import uuid4

os.environ.setdefault("GOOGLE_API_KEY", "test-key-not-used")

import httpx
import pytest

from src.services import InboundEmailService, MailboxService, OnboardingService

ADMIN = {"x-admin-token": "admin-secret"}


@pytest.fixture
async def client(monkeypatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "admin-secret")
    monkeypatch.setenv("PUBSUB_PUSH_TOKEN", "push-secret")
    from main import app
    from tests.conftest import reset_app_singletons

    reset_app_singletons()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c
    reset_app_singletons()


async def test_lifespan_starts_and_stops_the_background_workers(temp_database):
    """Startup runs the retry scheduler + call poller against the database and
    shutdown stops them cleanly (the pull subscriber is off by default)."""
    import asyncio

    from main import app, lifespan

    async with lifespan(app):
        await asyncio.sleep(0.3)        # let both loops run one tick against the empty database


async def test_health(client):
    response = await client.get("/health")
    assert response.status_code == 200 and response.json() == {"status": "ok"}


async def test_all_routes_are_registered(client):
    paths = {route.path for route in client._transport.app.routes}
    for expected in ("/health", "/webhooks/bolna", "/onboardings", "/onboardings/{onboarding_id}",
                     "/onboardings/{onboarding_id}/plan-tier", "/gmail/watch", "/pubsub/push"):
        assert expected in paths


@pytest.mark.parametrize("method, path", [
    ("GET", "/onboardings"),
    ("GET", f"/onboardings/{uuid4()}"),
    ("POST", "/gmail/watch"),
])
async def test_operator_routes_require_the_admin_token(client, method, path):
    response = await client.request(method, path, headers={"x-admin-token": "wrong"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
    assert (await client.request(method, path)).status_code == 401


async def test_unconfigured_admin_token_fails_closed(client, monkeypatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "")
    from tests.conftest import reset_app_singletons

    reset_app_singletons()
    assert (await client.get("/onboardings", headers={"x-admin-token": ""})).status_code == 401


async def test_pubsub_push_requires_its_token_and_a_real_envelope(client):
    assert (await client.post("/pubsub/push", json={"message": {"data": "e30="}})).status_code == 401
    assert (await client.post("/pubsub/push?token=wrong", json={})).status_code == 401
    bad = await client.post("/pubsub/push?token=push-secret", json={"nope": 1})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "bad_request"


async def test_manual_plan_tier_validates_the_body(client):
    url = f"/onboardings/{uuid4()}/plan-tier"
    invalid = await client.post(url, json={"plan_tier": "platinum", "reviewer": "a@b.co"}, headers=ADMIN)
    assert invalid.status_code == 422                                  # not an allowed PlanTierCode


async def test_onboarding_read_endpoints(client, temp_database):
    from tests.conftest import reset_app_singletons

    reset_app_singletons()                                              # pick up the temp database + tokens
    mailbox = await MailboxService().get_or_create("cs@x.co")
    inbound = await InboundEmailService().try_insert(mailbox.id, "g1", None, "ae@x.co", "Closed", None)
    oid = str(uuid4())
    await OnboardingService().create(oid, inbound.id, "Acme Corp", "jane@acme.co", "Jane", None, "006X", {})

    listing = await client.get("/onboardings", headers=ADMIN)
    assert listing.status_code == 200
    assert [o["id"] for o in listing.json()] == [oid]
    assert listing.json()[0]["status"] == "received" and "extra_details" not in listing.json()[0]   # a DTO, not the row

    assert (await client.get("/onboardings?status=completed", headers=ADMIN)).json() == []
    assert (await client.get("/onboardings?status=bogus", headers=ADMIN)).status_code == 422

    one = await client.get(f"/onboardings/{oid}", headers=ADMIN)
    assert one.status_code == 200 and one.json()["customer_name"] == "Acme Corp"

    missing = await client.get(f"/onboardings/{uuid4()}", headers=ADMIN)
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"

    not_waiting = await client.post(f"/onboardings/{oid}/plan-tier", json={"plan_tier": "growth", "reviewer": "a@b.co"}, headers=ADMIN)
    assert not_waiting.status_code == 409 and not_waiting.json()["error"]["code"] == "conflict"
