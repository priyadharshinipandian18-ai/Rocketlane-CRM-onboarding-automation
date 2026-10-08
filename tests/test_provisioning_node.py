"""``create_rocketlane_project_node``: the project is created first, then the template is imported, and what a
retry repeats. No database, no network: the services and the Rocketlane client are replaced."""
import os
from types import SimpleNamespace

os.environ.setdefault("GOOGLE_API_KEY", "test-key-not-used")

import pytest

from src.agents.provisioning import nodes
from src.models.entities import RocketlaneProject
from src.services import (
    AuditEventService,
    CsTeamMemberService,
    OnboardingService,
    PlanTierService,
    RocketlaneProjectService,
)

ONBOARDING_ID = "11111111-0000-4000-8000-000000000001"
STATE = {"onboarding_id": ONBOARDING_ID, "plan_tier": "growth", "customer_name": "Acme Corp"}


class FakeRocketlane:
    calls: list[tuple] = []
    fail_create: Exception | None = None
    fail_import: Exception | None = None

    async def create_project(self, **kwargs):
        FakeRocketlane.calls.append(("create", kwargs))
        if FakeRocketlane.fail_create:
            raise FakeRocketlane.fail_create
        return {"projectId": 4242, "projectUrl": "https://rl.example/p/4242"}

    async def import_template(self, project_id, template_id, start_date):
        FakeRocketlane.calls.append(("import", project_id, template_id))
        if FakeRocketlane.fail_import:
            raise FakeRocketlane.fail_import


@pytest.fixture
def world(monkeypatch):
    FakeRocketlane.calls, FakeRocketlane.fail_create, FakeRocketlane.fail_import = [], None, None
    db = SimpleNamespace(existing=None, saved=[], created=[], failed=[], notes=[], events=[], status=None)

    async def get_existing(self, _id):
        return db.existing

    async def create_pending(self, _id, template_id, payload):
        db.existing = RocketlaneProject(onboarding_id=_id, template_id=template_id, request_payload=payload)

    async def save_project_id(self, _id, project_id, url):
        db.saved.append(project_id)
        db.existing = db.existing.model_copy(update={"rocketlane_project_id": project_id, "project_url": url})

    async def mark_created(self, _id, **kwargs):
        db.created.append(kwargs["rocketlane_project_id"])
        db.existing = db.existing.model_copy(update={"status": "created"})

    async def mark_failed(self, _id, error):
        db.failed.append(error)
        db.existing = db.existing.model_copy(update={"status": "failed"})

    async def note_error(self, _id, error):
        db.notes.append(error)

    async def log_event(self, _id, actor, event_type, payload=None):
        db.events.append(event_type)

    async def update_status(self, _id, status, reason=None):
        db.status = getattr(status, "value", status)

    async def noop(self, *args, **kwargs):
        return None

    async def get_tier(self, code):
        return SimpleNamespace(rocketlane_template_id="222", duration_days=14)

    async def team(self, pool):
        return (SimpleNamespace(id="csm-1", email="csm@x.test", rocketlane_user_id="11"),
                SimpleNamespace(id="mgr-1", email="mgr@x.test", rocketlane_user_id="12"))

    monkeypatch.setattr(RocketlaneProjectService, "get_existing", get_existing)
    monkeypatch.setattr(RocketlaneProjectService, "create_pending", create_pending)
    monkeypatch.setattr(RocketlaneProjectService, "save_project_id", save_project_id)
    monkeypatch.setattr(RocketlaneProjectService, "mark_created", mark_created)
    monkeypatch.setattr(RocketlaneProjectService, "mark_failed", mark_failed)
    monkeypatch.setattr(OnboardingService, "update_status", update_status)
    monkeypatch.setattr(RocketlaneProjectService, "note_error", note_error)
    monkeypatch.setattr(OnboardingService, "set_team_and_dates", noop)
    monkeypatch.setattr(AuditEventService, "log_event", log_event)
    monkeypatch.setattr(PlanTierService, "get_by_code", get_tier)
    monkeypatch.setattr(CsTeamMemberService, "get_owner_and_manager", team)
    monkeypatch.setattr(nodes, "RocketlaneClient", FakeRocketlane)
    return db


def _kinds():
    return [c[0] for c in FakeRocketlane.calls]


async def test_the_project_is_created_first_and_the_tier_template_is_imported_into_it(world):
    out = await nodes.create_rocketlane_project_node(STATE)

    assert _kinds() == ["create", "import"]                                      # two calls, in this order
    create = FakeRocketlane.calls[0][1]
    assert "template_id" not in create                                           # nothing slow in the create call
    assert create["owner_email"] == "mgr@x.test" and create["external_reference_id"] == ONBOARDING_ID
    assert FakeRocketlane.calls[1] == ("import", "4242", "222")                  # the template comes from plan_tiers, for this project
    assert world.saved == ["4242"] and world.created == ["4242"]
    assert out["rocketlane_project_id"] == "4242" and world.status == "project_created"


async def test_a_failed_template_import_is_skipped_and_the_run_carries_on(world):
    """A 504 on the import must not stop the onboarding: the project exists, so Slack goes ahead, flagged for a human."""
    FakeRocketlane.fail_import = RuntimeError("504 gateway time-out")

    out = await nodes.create_rocketlane_project_node(STATE)                      # does not raise

    assert _kinds() == ["create", "import"]                                      # the import was attempted once, not retried
    assert out["rocketlane_project_id"] == "4242" and world.created == ["4242"]  # the project counts as created
    assert world.status == "project_created"
    assert any("Template import skipped: 504 gateway time-out" in note for note in world.notes)
    assert len(out["slack_warnings"]) == 1 and "NOT imported into project 4242" in out["slack_warnings"][0]
    assert out["node_reports"][0].status == "NEEDS_HUMAN" and "import it manually" in out["node_reports"][0].reasoning
    assert world.events == ["project_created", "template_import_skipped"]


async def test_a_clean_run_has_no_warning_and_no_skip_event(world):
    out = await nodes.create_rocketlane_project_node(STATE)
    assert "slack_warnings" not in out and world.notes == [] and world.events == ["project_created"]
    assert out["node_reports"][0].status == "RESOLVED"


async def test_a_failed_create_is_recorded_and_stops_before_any_import(world):
    FakeRocketlane.fail_create = RuntimeError("rocketlane down")
    with pytest.raises(nodes.ProvisioningError, match="rocketlane down"):
        await nodes.create_rocketlane_project_node(STATE)
    assert _kinds() == ["create"] and world.saved == [] and world.created == [] and world.status == "failed"


async def test_an_already_created_project_is_not_touched_again(world):
    world.existing = RocketlaneProject(onboarding_id=ONBOARDING_ID, template_id="222", rocketlane_project_id="77", status="created")
    out = await nodes.create_rocketlane_project_node(STATE)
    assert FakeRocketlane.calls == [] and out == {"rocketlane_project_id": "77"}
