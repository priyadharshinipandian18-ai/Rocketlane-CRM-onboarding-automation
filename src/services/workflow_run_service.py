"""Bookkeeping for workflow runs (see ``WorkflowRun``).

Every transition is a single conditional ``UPDATE``, so two racing callers (a
webhook and the poller, two app instances) can't both claim the same run. The
``waiting_for`` column is kept consistent with ``status`` by the table's check
constraint — every method here sets both together.
"""
from datetime import datetime

from src.models.entities import WorkflowRun
from src.models.enums import WaitingFor, WorkflowName, WorkflowRunStatus
from src.services.base_service import BaseService

_CLEAR = "waiting_for = NULL, due_at = NULL, stop_at = NULL"


class WorkflowRunService(BaseService[WorkflowRun]):
    def __init__(self) -> None:
        super().__init__(WorkflowRun)

    # ── creating / finding ────────────────────────────────────────────────

    async def create(
        self,
        workflow: WorkflowName | str,
        thread_id: str,
        onboarding_id: str | None = None,
        inbound_email_id: str | None = None,
        status: WorkflowRunStatus | str = WorkflowRunStatus.PENDING,
    ) -> WorkflowRun:
        """Idempotent per ``thread_id``: a second call returns the existing run."""
        created = await self.fetch_one(
            """
            INSERT INTO app.workflow_runs (workflow, thread_id, onboarding_id, inbound_email_id, status)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (thread_id) DO NOTHING
            RETURNING *
            """,
            (WorkflowName(workflow).value, thread_id, onboarding_id, inbound_email_id, WorkflowRunStatus(status).value),
        )
        if created is not None:
            return created
        existing = await self.get_by_thread(thread_id)
        assert existing is not None
        return existing

    async def get_by_thread(self, thread_id: str) -> WorkflowRun | None:
        return await self.find_one(thread_id=thread_id)

    async def get_for(self, onboarding_id: str, workflow: WorkflowName | str) -> WorkflowRun | None:
        return await self.find_one(onboarding_id=onboarding_id, workflow=WorkflowName(workflow).value)

    async def list_for_onboarding(self, onboarding_id: str) -> list[WorkflowRun]:
        return await self.find_all(order_by="started_at", onboarding_id=onboarding_id)

    async def attach_onboarding(self, run_id: str, onboarding_id: str) -> None:
        """The intake run learns which onboarding it created."""
        await self.execute(
            "UPDATE app.workflow_runs SET onboarding_id = %s, updated_at = now() WHERE id = %s", (onboarding_id, run_id)
        )

    # ── transitions made by the graph's own nodes ─────────────────────────

    async def mark_waiting(
        self,
        onboarding_id: str,
        workflow: WorkflowName | str,
        waiting_for: WaitingFor | str,
        current_node: str,
        due_at: datetime | None = None,
        stop_at: datetime | None = None,
    ) -> None:
        await self.execute(
            """
            UPDATE app.workflow_runs
            SET status = 'waiting', waiting_for = %s, current_node = %s, due_at = %s, stop_at = %s, updated_at = now()
            WHERE onboarding_id = %s AND workflow = %s
            """,
            (WaitingFor(waiting_for).value, current_node, due_at, stop_at, onboarding_id, WorkflowName(workflow).value),
        )

    # ── transitions made by the orchestrator ──────────────────────────────

    async def mark_succeeded(self, run_id: str, outcome: str | None) -> None:
        await self.execute(
            f"""
            UPDATE app.workflow_runs
            SET status = 'succeeded', {_CLEAR}, outcome = %s, last_error = NULL, finished_at = now(), updated_at = now()
            WHERE id = %s
            """,
            (outcome, run_id),
        )

    async def mark_failed(self, run_id: str, error: str, current_node: str | None = None) -> None:
        await self.execute(
            f"""
            UPDATE app.workflow_runs
            SET status = 'failed', {_CLEAR}, last_error = %s, current_node = COALESCE(%s, current_node),
                updated_at = now()
            WHERE id = %s
            """,
            (error[:1000], current_node, run_id),
        )

    # ── claims: atomically take a run out of a state ──────────────────────

    async def claim_pending(self, workflow: WorkflowName | str | None = None, limit: int = 20) -> list[WorkflowRun]:
        """``pending`` → ``running`` (the hand-off between workflows)."""
        return await self.fetch_all(
            f"""
            UPDATE app.workflow_runs
            SET status = 'running', {_CLEAR}, updated_at = now()
            WHERE id IN (
                SELECT id FROM app.workflow_runs
                WHERE status = 'pending' AND workflow <> 'intake' AND (%s::text IS NULL OR workflow = %s)
                ORDER BY started_at LIMIT %s
                FOR UPDATE SKIP LOCKED
            )
            RETURNING *
            """,
            (WorkflowName(workflow).value if workflow else None, WorkflowName(workflow).value if workflow else None, limit),
        )

    async def claim_waiting(
        self, onboarding_id: str, workflow: WorkflowName | str, waiting_for: WaitingFor | str
    ) -> WorkflowRun | None:
        """``waiting`` for exactly this → ``running``. A late or stray event for a
        run that is waiting for something else (or nothing) gets ``None``."""
        return await self.fetch_one(
            f"""
            UPDATE app.workflow_runs
            SET status = 'running', {_CLEAR}, updated_at = now()
            WHERE onboarding_id = %s AND workflow = %s AND status = 'waiting' AND waiting_for = %s
            RETURNING *
            """,
            (onboarding_id, WorkflowName(workflow).value, WaitingFor(waiting_for).value),
        )

    async def claim_for_tier_answer(self, onboarding_id: str) -> WorkflowRun | None:
        """The tier run is waiting for the AE's reply — or timed out waiting (``stopped``)
        but is still parked at the same node, so a late answer continues it."""
        return await self.fetch_one(
            f"""
            UPDATE app.workflow_runs
            SET status = 'running', {_CLEAR}, updated_at = now()
            WHERE onboarding_id = %s AND workflow = 'tier_confirmation'
              AND ((status = 'waiting' AND waiting_for = 'email_reply') OR status = 'stopped')
            RETURNING *
            """,
            (onboarding_id,),
        )

    async def claim_due_call_retries(self, limit: int = 20) -> list[WorkflowRun]:
        """Tier runs whose next call attempt is due → ``running``."""
        return await self.fetch_all(
            f"""
            UPDATE app.workflow_runs
            SET status = 'running', {_CLEAR}, updated_at = now()
            WHERE id IN (
                SELECT id FROM app.workflow_runs
                WHERE workflow = 'tier_confirmation' AND status = 'waiting' AND waiting_for = 'call_retry'
                  AND due_at <= now()
                ORDER BY due_at LIMIT %s
                FOR UPDATE SKIP LOCKED
            )
            RETURNING *
            """,
            (limit,),
        )

    async def claim_due_stops(self, limit: int = 20) -> list[WorkflowRun]:
        """Tier runs that waited too long for the AE's reply → ``stopped``. Nothing is
        lost: the graph stays parked at ``await_ae_reply`` in the checkpointer."""
        return await self.fetch_all(
            """
            UPDATE app.workflow_runs
            SET status = 'stopped', waiting_for = NULL, due_at = NULL, updated_at = now()
            WHERE id IN (
                SELECT id FROM app.workflow_runs
                WHERE workflow = 'tier_confirmation' AND status = 'waiting' AND waiting_for = 'email_reply'
                  AND stop_at <= now()
                ORDER BY stop_at LIMIT %s
                FOR UPDATE SKIP LOCKED
            )
            RETURNING *
            """,
            (limit,),
        )

    async def reopen_stopped(self, onboarding_id: str, stop_at: datetime) -> WorkflowRun | None:
        """``stopped`` → waiting for the AE's reply again, with a fresh deadline."""
        return await self.fetch_one(
            """
            UPDATE app.workflow_runs
            SET status = 'waiting', waiting_for = 'email_reply', current_node = 'await_ae_reply',
                stop_at = %s, updated_at = now()
            WHERE onboarding_id = %s AND workflow = 'tier_confirmation' AND status = 'stopped'
            RETURNING *
            """,
            (stop_at, onboarding_id),
        )

    async def claim_failed(self, onboarding_id: str, workflow: WorkflowName | str) -> WorkflowRun | None:
        """Manual retry: ``failed`` → ``running`` (the graph re-runs the node that failed)."""
        return await self.fetch_one(
            f"""
            UPDATE app.workflow_runs
            SET status = 'running', {_CLEAR}, attempt_count = attempt_count + 1, updated_at = now()
            WHERE onboarding_id = %s AND workflow = %s AND status = 'failed'
            RETURNING *
            """,
            (onboarding_id, WorkflowName(workflow).value),
        )
