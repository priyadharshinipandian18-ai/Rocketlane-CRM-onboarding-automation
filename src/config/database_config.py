"""Process-wide async Postgres connection pool.

One pool backs both the application tables (``db/schema.sql``) and the
LangGraph checkpointer (``src/config/checkpointer_config.py``) — same
database, same connections. ``autocommit`` + ``row_factory=dict_row`` are
required by ``AsyncPostgresSaver`` (verified against its own
``from_conn_string`` implementation) and are what the services rely on:
every ``fetchone()``/``fetchall()`` row comes back as a dict that
``BaseModel.from_row`` turns into an entity.
"""
import logging

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from src.config.settings import get_settings

log = logging.getLogger(__name__)


class DatabasePool:
    def __init__(self):
        self._pool: AsyncConnectionPool | None = None

    async def get_pool(self) -> AsyncConnectionPool:
        if self._pool is None:
            settings = get_settings()
            self._pool = AsyncConnectionPool(
                conninfo=settings.postgres_uri,
                open=False,
                kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
            )
            await self._pool.open()
            log.info("Postgres pool opened")
        return self._pool

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None


db_pool = DatabasePool()
