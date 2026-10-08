import logging
from datetime import date, timedelta

from src.agents.commons.models import NodeReport
from src.agents.provisioning.state import ProvisioningState
from src.integrations.rocketlane_client import RocketlaneClient
from src.models.enums import OnboardingStatus
from src.services import (
    AuditEventService,
    CsTeamMemberService,
    OnboardingService,
    PlanTierService,
    RocketlaneProjectService,
)

log = logging.getLogger(__name__)


class ProvisioningError(RuntimeError):
    """A provisioning step failed. The run stops *at that node* (its checkpoint is kept);
    ``POST /onboardings/{id}/retry`` re-runs it from there once the cause is fixed."""


async def assign_team_node(state: ProvisioningState) -> dict:
    """Used when ``ROCKETLANE_ENABLED=false``: the Rocketlane step normally records the CSM / manager
    and the dates on the onboarding (the Slack step reads them there), so do just that part."""
    onboarding_id, plan_tier_code = state["onboarding_id"], state.get("plan_tier")
    tier = await PlanTierService().get_by_code(plan_tier_code) if plan_tier_code else None
    if tier is None:
        reason = f"Plan tier '{plan_tier_code}' is not configured in app.plan_tiers."
        await OnboardingService().update_status(onboarding_id, OnboardingStatus.NEEDS_HUMAN_REVIEW, reason)
        raise ProvisioningError(reason)
    csm, manager = await CsTeamMemberService().get_owner_and_manager(plan_tier_code)
    start = date.today()
    await OnboardingService().set_team_and_dates(
        onboarding_id,
        csm_id=csm.id if csm else None,
        manager_id=manager.id if manager else None,
        start_date=start,
        target_go_live_date=start + timedelta(days=tier.duration_days),
    )
    log.info("Team assigned (Rocketlane disabled) | onboarding=%s csm=%s manager=%s", onboarding_id,
             csm.email if csm else None, manager.email if manager else None)
    return {}


async def create_rocketlane_project_node(state: ProvisioningState) -> dict:
    """Create the onboarding project once the plan tier is known.

    Writes a ``creating`` row to ``app.rocketlane_projects`` *before* calling the API, and
    checks for an existing row first — a retry after a failure (or a crash) skips a project
    that was already created instead of making a second one.

    Any failure is recorded and then **raised**: the graph stops here with its checkpoint,
    and a retry re-runs exactly this node.
    """
    onboarding_id = state["onboarding_id"]
    onboardings, projects = OnboardingService(), RocketlaneProjectService()
    plan_tier_code = state.get("plan_tier")
    if plan_tier_code not in ("enterprise", "growth"):
        reason = "Refusing to create a Rocketlane project without a confirmed plan tier."
        log.error("%s onboarding=%s", reason, onboarding_id)
        await onboardings.update_status(onboarding_id, OnboardingStatus.FAILED, reason)
        raise ProvisioningError(reason)

    existing = await projects.get_existing(onboarding_id)
    if existing and existing.status == "created":
        log.info("Rocketlane project already created for onboarding %s — skipping", onboarding_id)
        return {"rocketlane_project_id": existing.rocketlane_project_id}

    tier = await PlanTierService().get_by_code(plan_tier_code)
    if tier is None:
        reason = f"Plan tier '{plan_tier_code}' is not configured in app.plan_tiers (template, duration, Slack settings)."
        log.error("%s onboarding=%s", reason, onboarding_id)
        await onboardings.update_status(onboarding_id, OnboardingStatus.NEEDS_HUMAN_REVIEW, reason)
        raise ProvisioningError(reason)
    csm, manager = await CsTeamMemberService().get_owner_and_manager(plan_tier_code)

    # The Rocketlane project owner is the CS manager (the CSM when the tier has no manager).
    owner = manager or csm

    start = date.today()
    due = start + timedelta(days=tier.duration_days)
    request_payload = {
        "customer_name": state["customer_name"],
        "customer_contact_email": csm.email if csm else None,
        "template_id": tier.rocketlane_template_id,
        "owner_email": owner.email if owner else None,
    }

    if not existing:
        await projects.create_pending(onboarding_id, tier.rocketlane_template_id, request_payload)

    client = RocketlaneClient()
    project_url = existing.project_url if existing else None

    # Two steps. 1) Create the project (empty, so the call is fast) and remember its id. 2) Import the tier's template
    # into it. Applying the template inside the create call is slow enough for Rocketlane's gateway to time out
    # (504), after which nobody knows whether the project exists; this way a slow import is retried on its own and
    # never creates a second project. Step 1 is skipped when an earlier attempt already got a project id.
    if existing and existing.rocketlane_project_id:
        project_id = existing.rocketlane_project_id
        log.info("Rocketlane project %s already exists for onboarding %s — not creating another", project_id, onboarding_id)
    else:
        try:
            project = await client.create_project(
                customer_name=state["customer_name"],
                customer_contact_email=csm.email if csm else "",     # the CSM's email, as requested
                owner_email=owner.email if owner else "",
                start_date=start,
                due_date=due,
                owner_user_id=owner.rocketlane_user_id if owner else None,
                external_reference_id=onboarding_id,
            )
            project_id = str(project["projectId"])
        except Exception as exc:
            log.exception("Rocketlane project creation failed: %s", exc)
            await projects.mark_failed(onboarding_id, str(exc))
            await onboardings.update_status(onboarding_id, OnboardingStatus.FAILED, f"Rocketlane project creation failed: {exc}")
            raise ProvisioningError(f"Rocketlane project creation failed: {exc}") from exc
        project_url = project.get("projectUrl")
        await projects.save_project_id(onboarding_id, project_id, project_url)

    # A template that can't be imported (Rocketlane's gateway timing out, say) does NOT stop the onboarding: the
    # project exists and the customer's Slack channel should still open. The skip is recorded, flagged for a
    # human (it reaches the Slack step as a warning, so the onboarding ends in needs_human_review) and the
    # template is imported by hand.
    template_warning: str | None = None
    try:
        await client.import_template(project_id, tier.rocketlane_template_id, start)
    except Exception as exc:
        template_warning = (
            f"Rocketlane template {tier.rocketlane_template_id} was NOT imported into project {project_id} "
            f"({type(exc).__name__}); import it manually in Rocketlane"
        )
        log.warning("%s: %s", template_warning, exc)
        await projects.note_error(onboarding_id, f"Template import skipped: {exc}")

    await projects.mark_created(
        onboarding_id,
        rocketlane_project_id=project_id,
        project_url=project_url,
        owner_user_id=owner.rocketlane_user_id if owner else None,
        manager_user_id=manager.rocketlane_user_id if manager else None,
        start_date=start,
        due_date=due,
    )
    await onboardings.set_team_and_dates(
        onboarding_id,
        csm_id=csm.id if csm else None,
        manager_id=manager.id if manager else None,
        start_date=start,
        target_go_live_date=due,
    )
    await onboardings.update_status(onboarding_id, OnboardingStatus.PROJECT_CREATED)
    audit = AuditEventService()
    await audit.log_event(
        onboarding_id, actor="intake_agent", event_type="project_created", payload={"rocketlane_project_id": project_id}
    )
    if template_warning:
        await audit.log_event(
            onboarding_id, actor="intake_agent", event_type="template_import_skipped",
            payload={"rocketlane_project_id": project_id, "template_id": tier.rocketlane_template_id},
        )

    reasoning = f"Created {plan_tier_code} onboarding project {project_id} for {state['customer_name']}."
    update = {
        "rocketlane_project_id": project_id,
        "node_reports": [
            NodeReport(
                producer="create_rocketlane_project",
                status="NEEDS_HUMAN" if template_warning else "RESOLVED",
                reasoning=f"{reasoning} {template_warning}." if template_warning else reasoning,
            )
        ],
    }
    if template_warning:
        update["slack_warnings"] = [template_warning]      # reaches the Slack step, so the onboarding ends up flagged for review
    return update
