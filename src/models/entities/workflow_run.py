from datetime import datetime

from src.models.base_model import BaseModel
from src.models.enums import WaitingFor, WorkflowName, WorkflowRunStatus


class WorkflowRun(BaseModel):
    """One run of one workflow — the bridge between the outside world (call
    results, email replies, timers) and a graph paused in the LangGraph
    checkpointer under ``thread_id``.

    The checkpoint holds *where the graph is*; this row holds what an outside
    event needs in order to **find** it: its status, what it waits for, and
    the deadlines.
    """

    __table_name__ = "workflow_runs"

    id: str | None = None
    workflow: WorkflowName
    thread_id: str
    inbound_email_id: str | None = None
    onboarding_id: str | None = None
    status: WorkflowRunStatus = WorkflowRunStatus.PENDING
    waiting_for: WaitingFor | None = None
    current_node: str | None = None
    due_at: datetime | None = None
    stop_at: datetime | None = None
    outcome: str | None = None
    attempt_count: int = 0
    last_error: str | None = None
    started_at: datetime | None = None
    updated_at: datetime | None = None
    finished_at: datetime | None = None
