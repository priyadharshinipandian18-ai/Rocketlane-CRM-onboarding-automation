"""Lazy, process-wide LangGraph checkpointer backed by the same Postgres
database as the application tables (mirrors rappit-ai-companion's
``postgres_checkpointer_config`` singleton pattern).

A durable checkpointer is required here, not the trivial in-memory one: the
deal-intake graph pauses mid-run (``interrupt()``) while waiting for the
AE's phone call to complete, which can take minutes and must survive this
process restarting in between. ``workflow_runs.thread_id`` (db/schema.sql)
is the key tying a paused run back to this checkpointer's state.
"""
import logging

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from src.config.database_config import db_pool

log = logging.getLogger(__name__)


ALLOWED_MSGPACK_MODULES = [
    ("src.agents.commons.models", "NodeReport"),
    ("src.agents.commons.guardrail.models", "GuardrailCheck"),
]
"""The only custom types stored in checkpoints (the reasoning trail). LangGraph warns about — and a
future version will refuse — deserializing types that aren't registered, which would make every paused
run impossible to resume. Add a type here if you ever put a new one into a graph state."""


def build_serde() -> JsonPlusSerializer:
    return JsonPlusSerializer(allowed_msgpack_modules=ALLOWED_MSGPACK_MODULES)


class CheckpointerConfig:
    def __init__(self):
        self._saver: AsyncPostgresSaver | None = None

    async def get_checkpointer(self) -> AsyncPostgresSaver:
        if self._saver is None:
            pool = await db_pool.get_pool()
            self._saver = AsyncPostgresSaver(pool, serde=build_serde())
            await self._saver.setup()
            log.info("Postgres checkpointer ready")
        return self._saver


checkpointer_config = CheckpointerConfig()
