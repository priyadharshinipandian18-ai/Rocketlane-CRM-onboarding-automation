"""``POST /webhooks/bolna?token=...`` — OPTIONAL webhook mode for AE-call results.

The default is polling (``CallResultService.poll_in_flight_once``), which needs
no public URL; this endpoint is disabled unless ``BOLNA_WEBHOOK_TOKEN`` is set.
Bolna sends one per status change; only terminal statuses resume the graph, and
only once (shared logic: ``CallResultService``).
"""
import logging
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Header, Query, Request

from src.api.constants import X_WEBHOOK_TOKEN
from src.api.dependencies import client_ip, token_ok
from src.api.exceptions import AppException, ErrorCode
from src.config.settings import get_settings
from src.integrations.voice_ai_client import BolnaVoiceClient, is_terminal, map_call_status
from src.models.entities import WorkflowRun
from src.services import VoiceCallService
from src.services.call_result_service import CallResultService
from src.services.workflow_orchestrator import WorkflowOrchestrator

log = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


async def resume_run(run: WorkflowRun, payload: dict[str, Any]) -> None:
    """Run in the background so the request returns immediately. ``run`` was already claimed."""
    try:
        await WorkflowOrchestrator().resume(run, payload)
    except Exception:
        log.exception("Resuming %s failed", run.thread_id)


@router.post("/bolna")
async def bolna_webhook(
    request: Request,
    background: BackgroundTasks,
    token: Optional[str] = Query(default=None),
    x_webhook_token: Optional[str] = Header(default=None, alias=X_WEBHOOK_TOKEN),
) -> dict:
    settings = get_settings()
    if not token_ok(settings.bolna_webhook_token, token, x_webhook_token):
        raise AppException(ErrorCode.UNAUTHORIZED, "invalid webhook token")
    allowed_ips = {ip.strip() for ip in settings.bolna_allowed_ips.split(",") if ip.strip()}
    if allowed_ips and client_ip(request) not in allowed_ips:
        raise AppException(ErrorCode.FORBIDDEN, "source IP not allowed")

    body = await request.json()
    execution_id = body.get("id") or body.get("execution_id")
    status = (body.get("status") or "").lower()
    if not execution_id:
        return {}

    voice_calls = VoiceCallService()
    if await voice_calls.get_by_provider_call_id(execution_id) is None:
        log.info("Webhook for unknown execution %s (status=%s) ignored", execution_id, status)
        return {}

    # ── Intermediate status: just track it ─────────────────────────────
    if not is_terminal(status):
        mapped = map_call_status(status)
        if mapped and mapped != "completed":
            await voice_calls.update_status(execution_id, mapped)
        return {}

    # ── Terminal: re-fetch from Bolna's API — webhooks aren't signed, so
    #    the body is only a trigger, never the source of truth. ──────────
    try:
        execution = await BolnaVoiceClient().get_execution(execution_id)
    except Exception as exc:
        log.error("Could not verify execution %s with Bolna: %s", execution_id, exc)
        raise AppException(ErrorCode.UPSTREAM_ERROR, "could not verify execution with Bolna")

    verified_status = (execution.get("status") or "").lower()
    if not is_terminal(verified_status):
        log.info("Execution %s not final yet per Bolna API (status=%s)", execution_id, verified_status)
        return {}

    to_resume = await CallResultService().apply_finished_execution(execution_id, execution, actor="webhook")
    if to_resume:
        background.add_task(resume_run, to_resume["run"], to_resume["result"])
    return {}
