"""Starts, resumes and retries the three workflows, and keeps ``workflow_runs`` truthful.

The graphs decide *what happens next*; this decides *when a run starts, how it is
resumed, and what its row says afterwards*:

* ``process_inbound_email`` — run the intake workflow for one email; if it was a closed
  deal, kick off the tier workflow.
* ``start_pending``         — start every ``pending`` run (the hand-offs: intake → tier,
  tier → provisioning). Also called by the sweeper, so a hand-off survives a crash.
* ``resume``                — continue a run paused at ``interrupt()`` (call result, retry
  timer, the AE's reply); on a finished tier run the provisioning run it queued starts next.
* ``retry``                 — re-run a ``failed`` run from the node that failed.

After every execution the run's row is updated from the graph's checkpoint: finished →
``succeeded`` (+ outcome); paused → the pausing node already recorded ``waiting``;
exception → ``failed`` at that node.
"""
import logging
from typing import Any

from src.agents.intake.graph import intake_graph
from src.agents.provisioning.graph import provisioning_graph
from src.agents.tier_confirmation.graph import tier_graph
from src.config.settings import get_settings
from src.models.entities import Onboarding, WorkflowRun
from src.models.enums import EmailKind, InboundEmailStatus, WorkflowName
from src.services import AccountExecutiveService, AuditEventService, InboundEmailService, OnboardingService, WorkflowRunService

log = logging.getLogger(__name__)

MAX_HANDOFF_ROUNDS = 10
"""Guard for the start-pending loop (tier success queues provisioning, which queues nothing)."""


class WorkflowOrchestrator:
    def __init__(
        self,
        workflow_runs: WorkflowRunService | None = None,
        onboardings: OnboardingService | None = None,
        account_executives: AccountExecutiveService | None = None,
        inbound_emails: InboundEmailService | None = None,
        audit: AuditEventService | None = None,
        intake: Any = intake_graph,
        tier: Any = tier_graph,
        provisioning: Any = provisioning_graph,
    ) -> None:
        self.workflow_runs = workflow_runs or WorkflowRunService()
        self.onboardings = onboardings or OnboardingService()
        self.account_executives = account_executives or AccountExecutiveService()
        self.inbound_emails = inbound_emails or InboundEmailService()
        self.audit = audit or AuditEventService()
        self._graphs = {
            WorkflowName.INTAKE.value: intake,
            WorkflowName.TIER_CONFIRMATION.value: tier,
            WorkflowName.PROVISIONING.value: provisioning,
        }

    def graph_for(self, workflow: str) -> Any:
        return self._graphs[WorkflowName(workflow).value]

    # ── intake ──────────────────────────────────────────────────────────────

    async def process_inbound_email(self, inbound_email_id: str, email: dict[str, Any]) -> dict[str, Any]:
        """Classify one email (intake workflow). ``email`` carries ``sender``, ``subject``, the
        already-redacted ``body`` and ``gmail_thread_id``. Returns the final state — notably
        ``kind`` and, for an escalation reply, ``escalation_id`` — or ``{}`` if it failed."""
        thread_id = f"intake:{inbound_email_id}"
        run = await self.workflow_runs.create(
            WorkflowName.INTAKE, thread_id, inbound_email_id=inbound_email_id, status="running"
        )
        graph = self.graph_for("intake")
        values = await self._execute(
            run, graph,
            lambda: graph.start(thread_id, {
                "inbound_email_id": inbound_email_id,
                "sender": email["sender"],
                "subject": email["subject"],
                "body": email.get("body", ""),
                "gmail_thread_id": email.get("gmail_thread_id"),
            }),
        )
        if values is None:
            await self.inbound_emails.update_classification(
                inbound_email_id, is_deal_email=False, classification={"error": "intake workflow failed"},
                status=InboundEmailStatus.FAILED,
            )
            return {}

        onboarding_id = values.get("onboarding_id")
        if onboarding_id:
            await self.workflow_runs.attach_onboarding(run.id, onboarding_id)
        if values.get("kind") == EmailKind.DEAL_CLOSED.value and onboarding_id:
            await self.start_pending()
        return values

    # ── tier / provisioning ─────────────────────────────────────────────────

    async def start_pending(self, workflow: str | None = None) -> int:
        """Start every ``pending`` run. Returns how many were started."""
        started = 0
        for _ in range(MAX_HANDOFF_ROUNDS):
            runs = await self.workflow_runs.claim_pending(workflow)
            if not runs:
                break
            for run in runs:
                await self._start(run)
                started += 1
        return started

    async def resume(self, run: WorkflowRun, payload: dict[str, Any]) -> None:
        """Continue ``run`` (already claimed → ``running``) from where it is parked."""
        graph = self.graph_for(run.workflow)
        await self._execute(run, graph, lambda: graph.resume(run.thread_id, payload))
        await self.start_pending()      # a finished tier run queued its provisioning run

    async def retry(self, onboarding_id: str, workflow: str = WorkflowName.PROVISIONING.value) -> bool:
        """Re-run a ``failed`` run from the node that failed. ``False`` if it isn't failed."""
        run = await self.workflow_runs.claim_failed(onboarding_id, workflow)
        if run is None:
            return False
        graph = self.graph_for(run.workflow)
        await self.audit.log_event(onboarding_id, "operator", "workflow_retry", {"workflow": run.workflow, "node": run.current_node})
        await self._execute(run, graph, lambda: graph.rerun(run.thread_id))
        await self.start_pending()
        return True

    # ── internals ───────────────────────────────────────────────────────────

    async def _start(self, run: WorkflowRun) -> None:
        onboarding = await self.onboardings.get_by_id(run.onboarding_id) if run.onboarding_id else None
        if onboarding is None:
            await self.workflow_runs.mark_failed(run.id, f"onboarding {run.onboarding_id} not found")
            return
        graph = self.graph_for(run.workflow)
        state = await self._tier_input(onboarding) if run.workflow == WorkflowName.TIER_CONFIRMATION.value \
            else self._provisioning_input(onboarding)
        await self._execute(run, graph, lambda: graph.start(run.thread_id, state))

    async def _tier_input(self, onboarding: Onboarding) -> dict[str, Any]:
        """The tier workflow's initial state, rebuilt from the database."""
        extra = onboarding.extra_details or {}
        ae = await self.account_executives.get_by_id(onboarding.ae_id) if onboarding.ae_id else None
        return {
            "onboarding_id": onboarding.id,
            "customer_name": onboarding.customer_name,
            "customer_contact_name": onboarding.customer_contact_name or "",
            "customer_contact_email": onboarding.customer_contact_email,
            "ae_id": onboarding.ae_id,
            "ae_name": extra.get("ae_name") or (ae.name if ae else ""),
            "ae_email": extra.get("ae_email") or (ae.email if ae else ""),
            "call_attempts": 0,
        }

    @staticmethod
    def _provisioning_input(onboarding: Onboarding) -> dict[str, Any]:
        """The provisioning workflow's initial state — tiny: who, and which tier."""
        return {
            "onboarding_id": onboarding.id,
            "plan_tier": onboarding.plan_tier,
            "customer_name": onboarding.customer_name,
            "customer_contact_name": onboarding.customer_contact_name or "",
            "customer_contact_email": onboarding.customer_contact_email,
        }

    async def _execute(self, run: WorkflowRun, graph: Any, action) -> dict[str, Any] | None:
        """Run ``action`` and record the result on the run's row. Never raises."""
        try:
            values = await action()
        except Exception as exc:
            node = await self._next_node(graph, run.thread_id)
            log.exception("%s run %s failed%s", run.workflow, run.thread_id, f" at {node}" if node else "")
            await self.workflow_runs.mark_failed(run.id, f"{type(exc).__name__}: {exc}", node)
            return None

        snapshot = await graph.snapshot(run.thread_id)
        if snapshot.next:
            # Paused at an interrupt() — the node that paused already recorded why it waits.
            return values
        await self.workflow_runs.mark_succeeded(run.id, self._outcome(run.workflow, values))
        return values

    @staticmethod
    async def _next_node(graph: Any, thread_id: str) -> str | None:
        try:
            snapshot = await graph.snapshot(thread_id)
            return snapshot.next[0] if snapshot.next else None
        except Exception:
            return None

    @staticmethod
    def _outcome(workflow: str, values: dict[str, Any]) -> str:
        if workflow == WorkflowName.INTAKE.value:
            return values.get("kind") or "unclassified"
        if workflow == WorkflowName.TIER_CONFIRMATION.value:
            return f"tier:{values.get('plan_tier')}"
        clean = values.get("slack_channel_id") and not values.get("slack_warnings")
        if not get_settings().rocketlane_enabled:
            return "channel" if clean else "channel needs review"
        if not values.get("rocketlane_project_id"):
            return "no_project"
        return "project+channel" if clean else "project (channel needs review)"
