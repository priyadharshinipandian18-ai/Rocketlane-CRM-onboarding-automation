"""Rocketlane and Slack HTTP clients — the exact requests they send and how they report failures.
No network: ``httpx.AsyncClient`` is swapped for one backed by ``httpx.MockTransport``."""
import os
from datetime import date

os.environ.setdefault("GOOGLE_API_KEY", "test-key-not-used")

import httpx
import pytest

from src.integrations import rocketlane_client, slack_client
from src.integrations.rocketlane_client import RocketlaneClient, RocketlaneError
from src.integrations.slack_client import SlackApiError, SlackClient


@pytest.fixture
def http(monkeypatch):
    """Route every client's HTTP through a handler; returns the list of requests it saw."""
    class Seen(list):
        def respond(self, handler):
            state["handler"] = handler

    seen = Seen()
    state = {"handler": lambda request: httpx.Response(200, json={})}
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        def handle(request):
            seen.append(request)
            return state["handler"](request)

        return real_client(transport=httpx.MockTransport(handle), **{k: v for k, v in kwargs.items() if k != "transport"})

    monkeypatch.setattr(rocketlane_client.httpx, "AsyncClient", factory)
    monkeypatch.setattr(slack_client.httpx, "AsyncClient", factory)
    return seen


# ── Rocketlane ──────────────────────────────────────────────────────────────

async def test_create_project_creates_a_missing_company_and_sends_the_owner_user_id_when_numeric(http):
    import json

    http.respond(lambda r: httpx.Response(200, json={"projectId": 7}))
    client = RocketlaneClient()
    await client.create_project("Acme", "csm@x.test", "csm@x.test", date(2026, 10, 8), date(2026, 11, 7), owner_user_id="201")
    await client.create_project("Acme", "csm@x.test", "csm@x.test", date(2026, 10, 8), date(2026, 11, 7), owner_user_id="rl-asha")
    numeric, placeholder = (json.loads(r.content) for r in http)
    assert numeric["autoCreateCompany"] is True and numeric["owner"] == {"emailId": "csm@x.test", "userId": 201}
    assert placeholder["owner"] == {"emailId": "csm@x.test"}            # a non-numeric ID is never sent


async def test_create_project_applies_the_template_in_the_same_call(http):
    import json

    http.respond(lambda r: httpx.Response(200, json={"projectId": 7}))
    await RocketlaneClient().create_project(
        "Acme", "csm@x.test", "csm@x.test", date(2026, 10, 8), date(2026, 11, 7), template_id="222",
    )
    assert json.loads(http[0].content)["sources"] == [{"templateId": 222, "startDate": "2026-10-08"}]


async def test_create_project_without_a_template_sends_no_sources_and_the_api_key(http):
    http.respond(lambda r: httpx.Response(200, json={"projectId": 7, "projectUrl": "https://rl/7"}))
    project = await RocketlaneClient().create_project(
        customer_name="Acme", customer_contact_email="jane@acme.test", owner_email="csm@x.test",
        start_date=date(2026, 10, 8), due_date=date(2026, 11, 7),
    )
    request = http[0]
    assert project["projectId"] == 7 and request.method == "POST" and request.url.path.endswith("/projects")
    assert "api-key" in request.headers
    import json

    body = json.loads(request.content)
    assert body["projectName"] == "Acme Onboarding" and body["owner"] == {"emailId": "csm@x.test"}
    assert (body["startDate"], body["dueDate"]) == ("2026-10-08", "2026-11-07")
    assert "sources" not in body                                  # the template is imported as a separate second call


@pytest.mark.parametrize("template_id, expected", [("222", 222), (333, 333), ("tpl-abc", "tpl-abc")])
async def test_import_template_sends_a_json_array_with_an_integer_template_id(http, template_id, expected):
    import json

    http.respond(lambda r: httpx.Response(200, json={"projectId": 7}))
    await RocketlaneClient().import_template(7, template_id, date(2026, 10, 8))
    request = http[0]
    assert request.url.path.endswith("/projects/7/import-template")
    assert json.loads(request.content) == [{"templateId": expected, "startDate": "2026-10-08"}]


async def test_a_rocketlane_error_response_carries_the_explanation(http):
    http.respond(lambda r: httpx.Response(400, text='{"message":"templateId is invalid"}'))
    with pytest.raises(RocketlaneError, match="400.*templateId is invalid"):
        await RocketlaneClient().import_template(7, "1", date(2026, 10, 8))


async def test_a_read_timeout_warns_that_rocketlane_may_have_acted_anyway(http):
    def slow(request):
        raise httpx.ReadTimeout("")

    http.respond(slow)
    with pytest.raises(RocketlaneError, match=r"ReadTimeout.*STILL have processed"):
        await RocketlaneClient().create_project("Acme", "j@a.test", "o@x.test", date(2026, 10, 8), date(2026, 11, 7))


async def test_a_gateway_timeout_warns_that_rocketlane_may_have_acted_anyway(http):
    http.respond(lambda r: httpx.Response(504, text="<html><title>504 Gateway Time-out</title></html>"))
    with pytest.raises(RocketlaneError, match=r"504 gateway error.*STILL have processed") as caught:
        await RocketlaneClient().create_project("Acme", "j@a.test", "o@x.test", date(2026, 10, 8), date(2026, 11, 7))
    assert "<html>" not in str(caught.value)                            # the HTML error page is not dumped into the log


async def test_create_project_carries_our_onboarding_id_as_the_external_reference(http):
    import json

    http.respond(lambda r: httpx.Response(200, json={"projectId": 7}))
    await RocketlaneClient().create_project(
        "Acme", "j@a.test", "o@x.test", date(2026, 10, 8), date(2026, 11, 7), external_reference_id="onb-123"
    )
    assert json.loads(http[0].content)["externalReferenceId"] == "onb-123"


async def test_a_rocketlane_network_failure_names_the_host_instead_of_an_empty_message(http):
    def boom(request):
        raise httpx.ConnectTimeout("")

    http.respond(boom)
    with pytest.raises(RocketlaneError, match=r"could not reach api\.rocketlane\.com \(ConnectTimeout\)"):
        await RocketlaneClient().create_project("Acme", "j@a.test", "o@x.test", date(2026, 10, 8), date(2026, 11, 7))


# ── Slack ───────────────────────────────────────────────────────────────────

async def test_slack_missing_scope_says_which_scope_is_needed(http):
    http.respond(lambda r: httpx.Response(200, json={"ok": False, "error": "missing_scope", "needed": "groups:write", "provided": "chat:write"}))
    with pytest.raises(SlackApiError) as caught:
        await SlackClient().create_channel("nova-acme")
    assert caught.value.error.startswith("missing_scope") and "groups:write" in str(caught.value) and "chat:write" in str(caught.value)


async def test_lookup_by_email_uses_form_encoding_and_returns_the_member_id(http):
    http.respond(lambda r: httpx.Response(200, json={"ok": True, "user": {"id": "U42"}}))
    assert await SlackClient().lookup_user_id_by_email("csm@x.test") == "U42"
    request = http[0]
    assert request.url.path.endswith("/users.lookupByEmail")
    assert request.headers["content-type"].startswith("application/x-www-form-urlencoded")
    assert b"email=csm%40x.test" in request.content


async def test_lookup_by_email_returns_none_for_someone_who_is_not_a_member(http):
    http.respond(lambda r: httpx.Response(200, json={"ok": False, "error": "users_not_found"}))
    assert await SlackClient().lookup_user_id_by_email("ghost@x.test") is None


async def test_lookup_by_email_still_raises_other_slack_errors(http):
    http.respond(lambda r: httpx.Response(200, json={"ok": False, "error": "missing_scope"}))
    with pytest.raises(SlackApiError):
        await SlackClient().lookup_user_id_by_email("csm@x.test")
