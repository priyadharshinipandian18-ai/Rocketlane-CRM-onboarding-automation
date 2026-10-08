"""Generic data-access service — the project's equivalent of the shared
``BaseService`` in rappit-ai-companion, over async psycopg instead of Firestore.

A concrete service passes its entity class to ``super().__init__`` and gets
typed CRUD for free; anything more specific (an idempotent insert, a claim
with ``FOR UPDATE SKIP LOCKED``) is a method on the subclass that uses
``fetch_one`` / ``fetch_all`` / ``execute`` with its own SQL.

Column names in the generic helpers are checked against the entity's fields
and quoted, so a caller can't smuggle SQL through a keyword name.
"""
from typing import Any, Generic, TypeVar

from psycopg import sql
from psycopg.types.json import Jsonb

from src.config.database_config import db_pool
from src.models.base_model import BaseModel

T = TypeVar("T", bound=BaseModel)


class BaseService(Generic[T]):
    def __init__(self, entity: type[T]) -> None:
        self.entity = entity

    # ── low-level helpers for subclasses ──────────────────────────────────

    async def fetch_one(self, query: str, params: tuple | list = ()) -> T | None:
        pool = await db_pool.get_pool()
        async with pool.connection() as conn:
            return self.entity.from_row(await (await conn.execute(query, params)).fetchone())

    async def fetch_all(self, query: str, params: tuple | list = ()) -> list[T]:
        pool = await db_pool.get_pool()
        async with pool.connection() as conn:
            rows = await (await conn.execute(query, params)).fetchall()
        return [self.entity.from_row(row) for row in rows]

    async def execute(self, query: str, params: tuple | list = ()) -> None:
        pool = await db_pool.get_pool()
        async with pool.connection() as conn:
            await conn.execute(query, params)

    # ── generic CRUD ──────────────────────────────────────────────────────

    def _table(self) -> sql.Composed:
        return sql.SQL("{}.{}").format(sql.Identifier(self.entity.__schema__), sql.Identifier(self.entity.__table_name__))

    def _check_columns(self, names) -> None:
        unknown = set(names) - set(self.entity.model_fields)
        if unknown:
            raise ValueError(f"{self.entity.__name__} has no column(s) {sorted(unknown)}")

    @staticmethod
    def _db_value(value: Any) -> Any:
        return Jsonb(value) if isinstance(value, (dict, list)) else value

    async def get_by_id(self, entity_id: str) -> T | None:
        pk = self.entity.__primary_key__
        query = sql.SQL("SELECT * FROM {} WHERE {} = %s").format(self._table(), sql.Identifier(pk))
        return await self.fetch_one(query, (entity_id,))

    async def find_one(self, **filters: Any) -> T | None:
        found = await self.find_all(limit=1, **filters)
        return found[0] if found else None

    async def find_all(self, order_by: str | None = None, limit: int | None = None, **filters: Any) -> list[T]:
        self._check_columns(filters)
        query = sql.SQL("SELECT * FROM {}").format(self._table())
        if filters:
            query += sql.SQL(" WHERE ") + sql.SQL(" AND ").join(
                sql.SQL("{} = %s").format(sql.Identifier(col)) for col in filters
            )
        if order_by:
            self._check_columns([order_by.lstrip("-")])
            query += sql.SQL(" ORDER BY {} {}").format(
                sql.Identifier(order_by.lstrip("-")), sql.SQL("DESC" if order_by.startswith("-") else "ASC")
            )
        if limit:
            query += sql.SQL(" LIMIT {}").format(sql.Literal(int(limit)))
        return await self.fetch_all(query, list(filters.values()))

    async def save(self, entity: T) -> T:
        """INSERT the entity's set fields (``None`` is left to the column
        default) and return the stored row."""
        values = {k: v for k, v in entity.model_dump().items() if v is not None}
        self._check_columns(values)
        query = sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING *").format(
            self._table(),
            sql.SQL(", ").join(sql.Identifier(c) for c in values),
            sql.SQL(", ").join(sql.Placeholder() for _ in values),
        )
        stored = await self.fetch_one(query, [self._db_value(v) for v in values.values()])
        assert stored is not None  # INSERT ... RETURNING always yields the row
        return stored

    async def update(self, entity_id: str, **fields: Any) -> T | None:
        self._check_columns(fields)
        if not fields:
            return await self.get_by_id(entity_id)
        query = sql.SQL("UPDATE {} SET {} WHERE {} = %s RETURNING *").format(
            self._table(),
            sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(c)) for c in fields),
            sql.Identifier(self.entity.__primary_key__),
        )
        return await self.fetch_one(query, [*(self._db_value(v) for v in fields.values()), entity_id])

    async def delete(self, entity_id: str) -> None:
        query = sql.SQL("DELETE FROM {} WHERE {} = %s").format(self._table(), sql.Identifier(self.entity.__primary_key__))
        await self.execute(query, (entity_id,))
