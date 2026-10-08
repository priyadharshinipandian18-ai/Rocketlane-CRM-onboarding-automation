"""One lazily-compiled LangGraph workflow bound to the shared Postgres checkpointer.

Each *workflow* (intake / tier_confirmation / provisioning) is its own graph and
its runs live on their own **threads** (``tier:<onboarding_id>`` …), so a run's
position and state are stored and resumed independently of the others. This
wrapper offers the four things the orchestrator needs:

* ``start``  — begin a run with an initial state;
* ``resume`` — continue a run paused at ``interrupt()`` with the answer;
* ``rerun``  — continue a run that *failed*: LangGraph re-runs the node that
  raised, from the last good checkpoint;
* ``snapshot`` — where the run is now (``snapshot.next`` is empty once it finished).
"""
from typing import Any, Callable

from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from src.config.checkpointer_config import checkpointer_config


class WorkflowGraph:
    def __init__(self, build: Callable[[Any], CompiledStateGraph]) -> None:
        self._build = build
        self._graph: CompiledStateGraph | None = None

    async def compiled(self) -> CompiledStateGraph:
        """Built on first use — the checkpointer needs an awaited setup, so this can't happen at import."""
        if self._graph is None:
            self._graph = self._build(await checkpointer_config.get_checkpointer())
        return self._graph

    @staticmethod
    def _config(thread_id: str) -> dict:
        return {"configurable": {"thread_id": thread_id}}

    async def start(self, thread_id: str, state: dict[str, Any]) -> dict[str, Any]:
        return await (await self.compiled()).ainvoke(state, config=self._config(thread_id))

    async def resume(self, thread_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """``payload`` is whatever the paused node's ``interrupt()`` returns."""
        return await (await self.compiled()).ainvoke(Command(resume=payload), config=self._config(thread_id))

    async def rerun(self, thread_id: str) -> dict[str, Any]:
        return await (await self.compiled()).ainvoke(None, config=self._config(thread_id))

    async def snapshot(self, thread_id: str):
        return await (await self.compiled()).aget_state(self._config(thread_id))
