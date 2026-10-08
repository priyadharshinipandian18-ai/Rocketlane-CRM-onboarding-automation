"""What happens when an AE call has finished — shared by both ways a result
can arrive:

* the **poller** (``poll_in_flight_once``, driven by ``src/scheduler``) —
  default; asks Bolna for in-flight calls on a timer, needs no public URL;
* the **webhook** (``POST /webhooks/bolna``) — optional; Bolna pushes to us.

Both hand the authoritative execution (from ``GET /executions/{id}``) to
``apply_finished_execution``. It is safe to call more than once for the same
call (or from both paths at the same time): ``finalize_once`` lets exactly one
caller through, and only a tier run that is ``waiting`` for a ``call_result`` is
claimed and resumed.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from src.config.settings import get_settings
from src.integrations.voice_ai_client import (
    PROVIDER,
    BolnaVoiceClient,
    decide_outcome,
    is_terminal,
    map_call_status,
    parse_extraction,
    parse_transcript,
)
from src.models.entities import VoiceCall
from src.models.enums import WaitingFor, WorkflowName
from src.services import AuditEventService, VoiceCallService, WorkflowRunService
from src.services.workflow_orchestrator import WorkflowOrchestrator

log = logging.getLogger(__name__)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(float(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _age(call: VoiceCall, now: datetime) -> timedelta:
    started = call.started_at or call.created_at or now
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return now - started


class CallResultService:
    def __init__(self, orchestrator: WorkflowOrchestrator | None = None) -> None:
        self.voice_calls = VoiceCallService()
        self.workflow_runs = WorkflowRunService()
        self.audit = AuditEventService()
        self.orchestrator = orchestrator or WorkflowOrchestrator()

    async def apply_finished_execution(self, execution_id: str, execution: dict[str, Any], actor: str) -> dict | None:
        """Record a finished call and decide the outcome.

        Returns ``{"onboarding_id", "run", "result"}`` when the caller should resume the
        (already claimed) tier run with ``result`` via ``orchestrator.resume``; ``None``
        when there is nothing to do (already processed, unknown call, or the run isn't
        waiting for a call result).
        """
        settings = get_settings()
        status = (execution.get("status") or "").lower()
        telephony = execution.get("telephony_data") or {}

        call = await self.voice_calls.finalize_once(
            execution_id,
            ended_reason=status if status != "completed" else (telephony.get("hangup_reason") or "completed"),
            transcript=execution.get("transcript"),
            recording_url=telephony.get("recording_url"),
            duration_seconds=_int_or_none(execution.get("conversation_duration") or telephony.get("duration")),
            raw_webhook=execution,
        )
        if call is None:
            log.info("Execution %s already finalised (or unknown) — nothing to do", execution_id)
            return None

        extraction = parse_extraction(execution.get("extracted_data"))
        extraction["source"] = "bolna_extraction"
        if not execution.get("extracted_data") and settings.bolna_transcript_fallback:
            # Bolna returned NO extractions (the agent has none configured, or they didn't run) — read
            # the transcript instead, as strictly as the extraction would have.
            from_transcript = parse_transcript(execution.get("transcript"))
            if from_transcript["plan_tier"] != "unknown":
                log.warning("Execution %s had no Bolna extractions — tier '%s' read from the transcript "
                            "(configure the extractions in the Bolna agent: docs/bolna_agent_setup.md)",
                            execution_id, from_transcript["plan_tier"])
                extraction = {**from_transcript, "source": "transcript"}
        await self.voice_calls.record_tier_answer(
            execution_id,
            plan=extraction["plan_tier"],
            readback_confirmed=extraction["readback_confirmed"],
            notes=f"extracted plan value: {extraction['raw_plan_value']!r}" if extraction["raw_plan_value"] else None,
            confidence=extraction["confidence"],
        )
        result = decide_outcome(
            status,
            extraction["plan_tier"],
            extraction["readback_confirmed"],
            extraction["confidence"],
            settings.bolna_min_confidence,
            hangup_reason=telephony.get("hangup_reason"),
        )
        await self.voice_calls.set_outcome(call.id, result["outcome"])

        onboarding_id = str(call.onboarding_id)
        await self.audit.log_event(
            onboarding_id, actor=actor, event_type="call_finished",
            payload={"execution_id": execution_id, "status": status, **extraction, **result},
        )
        log.info("Call finished | onboarding=%s execution=%s via=%s result=%s", onboarding_id, execution_id, actor, result)

        run = await self.workflow_runs.claim_waiting(onboarding_id, WorkflowName.TIER_CONFIRMATION, WaitingFor.CALL_RESULT)
        if run is None:
            log.warning("Run for onboarding %s is not waiting for a call result — not resuming", onboarding_id)
            return None
        return {"onboarding_id": onboarding_id, "run": run, "result": result}

    async def poll_in_flight_once(self) -> int:
        """One polling pass over calls we placed that haven't finished.
        Returns how many paused runs were resumed.

        * still running → update ``voice_calls.status`` (ringing, in_progress…);
        * finished      → ``apply_finished_execution`` and resume the graph;
        * stuck too long → treated as a failed call, so the normal retry /
          human-fallback path takes over instead of waiting forever.
        """
        settings = get_settings()
        timeout = timedelta(minutes=settings.voice_call_timeout_minutes)
        calls = await self.voice_calls.list_in_flight(PROVIDER)
        if not calls:
            return 0

        client = BolnaVoiceClient()
        now = datetime.now(timezone.utc)
        resumed = 0

        for call in calls:
            execution_id = call.provider_call_id
            too_old = _age(call, now) >= timeout

            try:
                execution = await client.get_execution(execution_id)
            except Exception as exc:
                if not too_old:
                    log.warning("Polling execution %s failed (will retry next pass): %s", execution_id, exc)
                    continue
                execution = {"id": execution_id, "status": "failed",
                             "telephony_data": {"hangup_reason": f"timeout: Bolna unreachable ({exc})"[:200]}}

            status = (execution.get("status") or "").lower()
            if not is_terminal(status):
                if too_old:
                    log.warning("Execution %s stuck in '%s' for over %s — treating as failed", execution_id, status, timeout)
                    execution = {**execution, "status": "failed",
                                 "telephony_data": {**(execution.get("telephony_data") or {}),
                                                    "hangup_reason": f"timeout: still '{status}'"}}
                else:
                    mapped = map_call_status(status)
                    if mapped and mapped != "completed":
                        await self.voice_calls.update_status(execution_id, mapped)
                    continue

            to_resume = await self.apply_finished_execution(execution_id, execution, actor="poller")
            if to_resume:
                await self.orchestrator.resume(to_resume["run"], to_resume["result"])
                resumed += 1
        return resumed
