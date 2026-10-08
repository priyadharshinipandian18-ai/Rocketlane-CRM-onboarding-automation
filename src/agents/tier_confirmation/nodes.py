"""Nodes of the tier-confirmation workflow.

    place_plan_tier_call ──placed──► await_plan_tier_result (interrupt: call result)
         │  │  └─deferred (outside AE hours)──► wait_for_call_retry (interrupt: timer)
         │  └─failed (API error) ─┐                       ▲
         └─no_phone ──────────┐   ▼                       │
                              │  route_after_call ──not confirmed, attempts left──► schedule_call_retry
                              │       │ confirmed
                              │       ▼
                              │  confirm_tier ──► END  (hands off to the provisioning workflow)
                              └─► request_ae_reply ──► await_ae_reply (interrupt: the AE's email reply)
                                                          └──► confirm_tier

Every node that calls ``interrupt()`` has NO side effects before it — LangGraph re-runs
such a node from the top on resume. Each pause also records, in ``workflow_runs``, *why*
the run is waiting (``waiting_for``) and the deadline, so the outside world can find it.
"""
import logging
from datetime import datetime, timedelta, timezone

from langgraph.types import interrupt

from src.agents.commons.models import NodeReport
from src.agents.tier_confirmation.call_policy import next_call_time
from src.agents.tier_confirmation.state import TierConfirmationState
from src.config.settings import get_settings
from src.integrations.voice_ai_client import PROVIDER as VOICE_PROVIDER, BolnaVoiceClient
from src.models.enums import OnboardingStatus, PlanTierSource, WaitingFor, WorkflowName, WorkflowRunStatus
from src.services import (
    AccountExecutiveService,
    AuditEventService,
    EscalationService,
    OnboardingService,
    VoiceCallService,
    WorkflowRunService,
)
from src.services.human_tier_notifier import HumanTierNotifier

log = logging.getLogger(__name__)

TIER = WorkflowName.TIER_CONFIRMATION


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def place_plan_tier_call_node(state: TierConfirmationState) -> dict:
    """Place one call to the AE. Side-effecting half of the HITL pair — it
    never calls ``interrupt()``, so resuming the graph never re-dials.

    Idempotent per attempt: ``voice_calls`` has ``UNIQUE (onboarding_id,
    attempt_no)``; if this node is replayed after a crash and the attempt
    already has a provider call id, that call is reused instead of placing
    a second one.
    """
    settings = get_settings()
    onboarding_id = state["onboarding_id"]
    onboardings, workflow_runs, voice_calls, audit = (
        OnboardingService(), WorkflowRunService(), VoiceCallService(), AuditEventService(),
    )
    ae = await AccountExecutiveService().get_by_email(state["ae_email"]) if state.get("ae_email") else None

    if not ae:
        reason = f"No active account_executives row (phone number) for AE '{state.get('ae_email') or state.get('ae_name')}'."
        log.error(reason)
        await audit.log_event(onboarding_id, actor="intake_agent", event_type="call_not_possible", payload={"reason": reason})
        return {
            "call_placement": "no_phone",
            "node_reports": [NodeReport(producer="place_plan_tier_call", status="VOICE_CALL_FAILED", reasoning=reason)],
        }

    # Respect the AE's working hours — defer instead of calling at 2am.
    now = _now()
    allowed_at = next_call_time(
        now, ae.timezone, timedelta(0),
        settings.voice_call_window_start_hour, settings.voice_call_window_end_hour,
    )
    if allowed_at > now:
        log.info("Call to %s deferred until %s: outside the %02d:00-%02d:00 calling window in %s | onboarding=%s",
                 ae.name, allowed_at.isoformat(), settings.voice_call_window_start_hour,
                 settings.voice_call_window_end_hour, ae.timezone, onboarding_id)
        await workflow_runs.mark_waiting(
            onboarding_id, TIER, WaitingFor.CALL_RETRY, current_node="wait_for_call_retry", due_at=allowed_at
        )
        await onboardings.update_status(onboarding_id, OnboardingStatus.AWAITING_TIER_CONFIRMATION, "Waiting for AE working hours")
        await audit.log_event(
            onboarding_id, actor="intake_agent", event_type="call_deferred",
            payload={"until": allowed_at.isoformat(), "ae_timezone": ae.timezone},
        )
        return {
            "call_placement": "deferred",
            "next_call_at": allowed_at.isoformat(),
            "node_reports": [NodeReport(
                producer="place_plan_tier_call", status="CALL_DEFERRED",
                reasoning=f"Outside {ae.name}'s working hours ({ae.timezone}); call deferred to {allowed_at.isoformat()}.",
            )],
        }

    attempt_no = state.get("call_attempts", 0) + 1
    call = await voice_calls.create_attempt(onboarding_id, attempt_no, VOICE_PROVIDER, ae.phone_e164)

    if call.provider_call_id:
        log.info("Attempt %d already placed (call_id=%s) — not dialing again", attempt_no, call.provider_call_id)
        call_id = call.provider_call_id
    elif call.outcome == "failed":
        return {"call_attempts": attempt_no, "call_placement": "failed", "voice_call_id": None, "call_outcome": "failed"}
    else:
        try:
            call_id = await BolnaVoiceClient().send_call(
                phone_number=ae.phone_e164,
                ae_name=ae.name,
                customer_name=state.get("customer_name", ""),
                metadata={"onboarding_id": onboarding_id, "attempt_no": attempt_no},
            )
        except Exception as exc:
            log.exception("Placing the AE call failed: %s", exc)
            await voice_calls.mark_place_failed(call.id, str(exc))
            await audit.log_event(
                onboarding_id, actor="intake_agent", event_type="call_failed",
                payload={"attempt": attempt_no, "error": str(exc)[:500]},
            )
            return {
                "call_attempts": attempt_no,
                "call_placement": "failed",
                "voice_call_id": None,
                "call_outcome": "failed",
                "node_reports": [NodeReport(
                    producer="place_plan_tier_call", status="VOICE_CALL_FAILED",
                    reasoning=f"Attempt {attempt_no}: provider rejected the call: {exc}",
                )],
            }
        await voice_calls.set_provider_call_id(call.id, call_id)

    await onboardings.update_status(onboarding_id, OnboardingStatus.AWAITING_TIER_CONFIRMATION)
    await workflow_runs.mark_waiting(onboarding_id, TIER, WaitingFor.CALL_RESULT, current_node="await_plan_tier_result")
    await audit.log_event(
        onboarding_id, actor="intake_agent", event_type="call_started",
        payload={"call_id": call_id, "attempt": attempt_no},
    )
    return {
        "call_attempts": attempt_no,
        "call_placement": "placed",
        "voice_call_id": call_id,
        "call_outcome": None,
        "node_reports": [NodeReport(
            producer="place_plan_tier_call", status="AWAITING_VOICE_CALL",
            reasoning=f"Attempt {attempt_no}: call placed to {ae.name} ({ae.phone_e164}), call_id={call_id}.",
        )],
    }


def route_after_place(state: TierConfirmationState) -> str:
    placement = state.get("call_placement")
    if placement == "placed":
        return "await_plan_tier_result"
    if placement == "deferred":
        return "wait_for_call_retry"
    if placement == "no_phone":
        return "request_ae_reply"
    return route_after_call(state)   # "failed": same retry policy as an unanswered call


async def await_plan_tier_result_node(state: TierConfirmationState) -> dict:
    """Pure pause. Resumed (poller or webhook) with ``{"outcome": ..., "plan_tier": ...}``.
    Code after ``interrupt()`` runs only once, after the resume."""
    resume_payload = interrupt(
        {
            "type": "awaiting_voice_call_result",
            "call_id": state.get("voice_call_id"),
            "attempt": state.get("call_attempts"),
            "ae_name": state.get("ae_name"),
        }
    )
    onboarding_id = state["onboarding_id"]
    outcome = resume_payload.get("outcome", "unclear")

    if outcome == "confirmed" and resume_payload.get("plan_tier") in ("enterprise", "growth"):
        tier = resume_payload["plan_tier"]
        await OnboardingService().set_plan_tier(onboarding_id, tier, source=PlanTierSource.VOICE_CALL)
        await AuditEventService().log_event(
            onboarding_id, actor="intake_agent", event_type="tier_confirmed",
            payload={"plan_tier": tier, "source": "voice_call", "call_id": state.get("voice_call_id")},
        )
        return {
            "call_outcome": "confirmed",
            "plan_tier": tier,
            "plan_tier_source": "voice_call",
            "node_reports": [NodeReport(
                producer="await_plan_tier_result", status="TIER_CONFIRMED",
                reasoning=f"AE confirmed '{tier}' on attempt {state.get('call_attempts')} (read-back accepted).",
            )],
        }

    await AuditEventService().log_event(
        onboarding_id, actor="intake_agent", event_type="tier_not_confirmed",
        payload={"outcome": outcome, "attempt": state.get("call_attempts")},
    )
    return {
        "call_outcome": outcome if outcome in ("unclear", "no_answer", "voicemail", "failed") else "unclear",
        "node_reports": [NodeReport(
            producer="await_plan_tier_result", status="TIER_NOT_CONFIRMED",
            reasoning=f"Attempt {state.get('call_attempts')} ended without a confirmed tier (outcome={outcome}).",
        )],
    }


def route_after_call(state: TierConfirmationState) -> str:
    if state.get("call_outcome") == "confirmed" and state.get("plan_tier"):
        return "confirm_tier"
    if state.get("call_attempts", 0) < get_settings().voice_max_attempts:
        return "schedule_call_retry"
    return "request_ae_reply"


async def schedule_call_retry_node(state: TierConfirmationState) -> dict:
    """Pick the next attempt time (delay + AE working hours) and park the run.
    The retry scheduler resumes it when ``due_at`` passes."""
    settings = get_settings()
    onboarding_id = state["onboarding_id"]
    ae = await AccountExecutiveService().get_by_email(state["ae_email"]) if state.get("ae_email") else None
    retry_at = next_call_time(
        _now(),
        ae.timezone if ae else "UTC",
        timedelta(seconds=settings.voice_retry_delay_seconds)
        if settings.voice_retry_delay_seconds is not None
        else timedelta(minutes=settings.voice_retry_delay_minutes),
        settings.voice_call_window_start_hour,
        settings.voice_call_window_end_hour,
    )
    await WorkflowRunService().mark_waiting(
        onboarding_id, TIER, WaitingFor.CALL_RETRY, current_node="wait_for_call_retry", due_at=retry_at
    )
    await OnboardingService().update_status(
        onboarding_id, OnboardingStatus.AWAITING_TIER_CONFIRMATION,
        f"Attempt {state.get('call_attempts')} {state.get('call_outcome')}; retrying at {retry_at.isoformat()}",
    )
    await AuditEventService().log_event(
        onboarding_id, actor="intake_agent", event_type="call_retry_scheduled",
        payload={"after_attempt": state.get("call_attempts"), "retry_at": retry_at.isoformat()},
    )
    return {
        "next_call_at": retry_at.isoformat(),
        "node_reports": [NodeReport(
            producer="schedule_call_retry", status="RETRY_SCHEDULED",
            reasoning=f"Attempt {state.get('call_attempts')} of {settings.voice_max_attempts} not confirmed; next call at {retry_at.isoformat()}.",
        )],
    }


async def wait_for_call_retry_node(state: TierConfirmationState) -> dict:
    """Pure pause until the retry scheduler resumes with ``{"action": "retry"}``."""
    interrupt({"type": "awaiting_call_retry", "next_call_at": state.get("next_call_at")})
    return {"next_call_at": None}


async def request_ae_reply_node(state: TierConfirmationState) -> dict:
    """The call couldn't confirm the tier — ask the AE by email.

    Sends "reply ENTERPRISE or GROWTH", records the escalation, and parks the run at
    ``await_ae_reply``: ``waiting_for='email_reply'`` with a ``stop_at`` deadline
    (``HUMAN_STOP_AFTER_HOURS``). The AE's reply, or ``POST /onboardings/{id}/plan-tier``,
    continues it from there.
    """
    onboarding_id = state["onboarding_id"]
    if state.get("call_placement") == "no_phone":
        reason = "No phone number on file for the AE."
    else:
        reason = f"Plan tier not confirmed after {state.get('call_attempts', 0)} call attempt(s)."

    notifier = HumanTierNotifier()
    await OnboardingService().update_status(onboarding_id, OnboardingStatus.NEEDS_HUMAN_REVIEW, reason)
    await WorkflowRunService().mark_waiting(
        onboarding_id, TIER, WaitingFor.EMAIL_REPLY, current_node="await_ae_reply", stop_at=notifier.stop_at()
    )
    await AuditEventService().log_event(onboarding_id, actor="intake_agent", event_type="human_review_requested", payload={"reason": reason})
    escalation = await notifier.open_request(
        onboarding_id, state.get("ae_email"), state.get("ae_name", ""), state.get("customer_name", ""), reason
    )
    log.warning(
        "AE REPLY NEEDED | onboarding=%s customer=%s | %s | escalation emailed (%s) | the AE replies by email, or "
        "POST /onboardings/%s/plan-tier",
        onboarding_id, state.get("customer_name"), reason, escalation.token if escalation else "not sent", onboarding_id,
    )
    return {"node_reports": [NodeReport(producer="request_ae_reply", status="NEEDS_HUMAN", reasoning=reason)]}


async def await_ae_reply_node(state: TierConfirmationState) -> dict:
    """Pure pause. Resumed with ``{"plan_tier", "reviewer", "inbound_email_id"?}`` — by the
    AE's email reply, or by an operator through the API."""
    decision = interrupt({"type": "awaiting_ae_email_reply", "customer_name": state.get("customer_name")})
    tier = decision["plan_tier"]
    reviewer = decision.get("reviewer", "unknown")
    onboarding_id = state["onboarding_id"]

    await OnboardingService().set_plan_tier(onboarding_id, tier, source=PlanTierSource.MANUAL_OVERRIDE)
    await EscalationService().mark_answered(onboarding_id, tier, reviewer, decision.get("inbound_email_id"))
    await AuditEventService().log_event(
        onboarding_id, actor=f"human:{reviewer}", event_type="tier_confirmed",
        payload={"plan_tier": tier, "source": "manual_override"},
    )
    return {
        "plan_tier": tier,
        "plan_tier_source": "manual_override",
        "node_reports": [NodeReport(
            producer="await_ae_reply", status="TIER_CONFIRMED",
            reasoning=f"Tier '{tier}' confirmed by {reviewer}.",
        )],
    }


async def confirm_tier_node(state: TierConfirmationState) -> dict:
    """The tier is known (from the call or the AE's reply). Hand off: queue the provisioning
    workflow as a ``pending`` run — a durable record, so even if the app dies right here
    the orchestrator's sweeper still starts it."""
    onboarding_id = state["onboarding_id"]
    await WorkflowRunService().create(
        WorkflowName.PROVISIONING, f"provision:{onboarding_id}", onboarding_id=onboarding_id, status=WorkflowRunStatus.PENDING
    )
    await AuditEventService().log_event(
        onboarding_id, actor="intake_agent", event_type="tier_workflow_completed",
        payload={"plan_tier": state.get("plan_tier"), "source": state.get("plan_tier_source")},
    )
    return {
        "node_reports": [NodeReport(
            producer="confirm_tier", status="RESOLVED",
            reasoning=f"Tier '{state.get('plan_tier')}' confirmed; provisioning queued.",
        )]
    }
