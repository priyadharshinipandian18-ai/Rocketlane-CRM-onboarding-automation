"""Operator endpoints for onboardings. Every route needs the ``x-admin-token`` header."""
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Query

from src.api.dependencies import require_admin_token
from src.api.exceptions import AppException, ErrorCode
from src.api.routes.webhook_router import resume_run
from src.models.dto.request import ManualPlanTierRequest, ResumeOnboardingRequest
from src.models.dto.response import (
    OnboardingSummary,
    PlanTierResumeResponse,
    ResumeResponse,
    RetryResponse,
    WorkflowRunSummary,
)
from src.models.enums import OnboardingStatus, WorkflowName
from src.services import OnboardingService, WorkflowRunService
from src.services.tier_escalation_service import TierEscalationService
from src.services.workflow_orchestrator import WorkflowOrchestrator

router = APIRouter(prefix="/onboardings", tags=["onboardings"], dependencies=[Depends(require_admin_token)])


@router.get("", response_model=list[OnboardingSummary])
async def list_onboardings(
    status: Optional[OnboardingStatus] = Query(default=None), limit: int = Query(default=50, ge=1, le=200)
) -> list[OnboardingSummary]:
    onboardings = await OnboardingService().list_by_status(status, limit)
    return [OnboardingSummary.from_entity(o) for o in onboardings]


@router.get("/{onboarding_id}", response_model=OnboardingSummary)
async def get_onboarding(onboarding_id: str) -> OnboardingSummary:
    onboarding = await OnboardingService().get_by_id(onboarding_id)
    if onboarding is None:
        raise AppException(ErrorCode.NOT_FOUND, f"onboarding {onboarding_id} not found")
    return OnboardingSummary.from_entity(onboarding)


@router.get("/{onboarding_id}/runs", response_model=list[WorkflowRunSummary])
async def list_workflow_runs(onboarding_id: str) -> list[WorkflowRunSummary]:
    """Where each part of this onboarding is: tier confirmation, then provisioning."""
    runs = await WorkflowRunService().list_for_onboarding(onboarding_id)
    return [WorkflowRunSummary.model_validate(r) for r in runs]


@router.post("/{onboarding_id}/plan-tier", response_model=PlanTierResumeResponse)
async def set_plan_tier_manually(
    onboarding_id: str, body: ManualPlanTierRequest, background: BackgroundTasks
) -> PlanTierResumeResponse:
    """An operator sets the tier by hand once the call and the AE's email have gone unanswered.
    Continues the tier run from where it is parked — once. Also works on a *stopped* run."""
    run = await WorkflowRunService().claim_for_tier_answer(onboarding_id)
    if run is None:
        raise AppException(ErrorCode.CONFLICT, "This onboarding is not waiting for a plan tier from a person.")

    background.add_task(resume_run, run, {"plan_tier": body.plan_tier, "reviewer": body.reviewer})
    return PlanTierResumeResponse(onboarding_id=onboarding_id, plan_tier=body.plan_tier)


@router.post("/{onboarding_id}/resume", response_model=ResumeResponse)
async def resume_onboarding(
    onboarding_id: str, background: BackgroundTasks, body: ResumeOnboardingRequest | None = None
) -> ResumeResponse:
    """Continue a tier run that stopped waiting for the AE's reply — from where it stopped.

    * body ``{"plan_tier": ..., "reviewer": ...}`` → apply that answer and carry on
      (Rocketlane project, Slack channel);
    * empty body → re-open the request: restart the clock and email the AE again.
    """
    body = body or ResumeOnboardingRequest()
    if body.plan_tier:
        run = await WorkflowRunService().claim_for_tier_answer(onboarding_id)
        if run is None:
            raise AppException(ErrorCode.CONFLICT, "This onboarding is not waiting for (or stopped on) a plan tier.")
        background.add_task(resume_run, run, {"plan_tier": body.plan_tier, "reviewer": body.reviewer})
        return ResumeResponse(status="resuming", onboarding_id=onboarding_id, plan_tier=body.plan_tier)

    if not await TierEscalationService().reopen(onboarding_id):
        raise AppException(ErrorCode.CONFLICT, "Only a stopped run can be re-opened; this one isn't stopped.")
    return ResumeResponse(status="reopened", onboarding_id=onboarding_id)


@router.post("/{onboarding_id}/retry", response_model=RetryResponse)
async def retry_failed_workflow(
    onboarding_id: str,
    background: BackgroundTasks,
    workflow: WorkflowName = Query(default=WorkflowName.PROVISIONING),
) -> RetryResponse:
    """Re-run a *failed* workflow from the node that failed (default: provisioning — the
    Rocketlane project / Slack channel). Fix the cause first (an API key, a missing plan tier)."""
    run = await WorkflowRunService().get_for(onboarding_id, workflow)
    if run is None or run.status != "failed":
        raise AppException(ErrorCode.CONFLICT, f"The {workflow.value} workflow of this onboarding is not in a failed state.")
    background.add_task(WorkflowOrchestrator().retry, onboarding_id, workflow.value)
    return RetryResponse(onboarding_id=onboarding_id, workflow=workflow)
