"""Base class for every model in the project — entities (one per ``app.*``
table), request/response DTOs and the agents' structured-output schemas.

It extends pydantic's ``BaseModel`` and adds the little the persistence layer
needs: table metadata and row → model conversion. Entities set
``__table_name__``; everything else leaves it ``None``.
"""
from collections.abc import Mapping
from typing import Any, ClassVar, Self
from uuid import UUID

from pydantic import BaseModel as PydanticBaseModel
from pydantic import ConfigDict


class BaseModel(PydanticBaseModel):
    model_config = ConfigDict(
        extra="ignore",           # SELECT * may return columns a model doesn't declare
        from_attributes=True,
        populate_by_name=True,
        use_enum_values=True,     # enum-typed fields hold their plain string value
    )

    __table_name__: ClassVar[str | None] = None
    __schema__: ClassVar[str] = "app"
    __primary_key__: ClassVar[str] = "id"

    @classmethod
    def from_row(cls, row: Mapping[str, Any] | None) -> Self | None:
        """Build a model from a ``dict_row``; ``None`` in, ``None`` out.

        UUID columns become ``str`` — ids travel through the graph state and
        the audit log as strings.
        """
        if row is None:
            return None
        return cls.model_validate({k: str(v) if isinstance(v, UUID) else v for k, v in row.items()})

    @classmethod
    def table_ref(cls) -> str:
        """Schema-qualified table name, e.g. ``app.onboardings``."""
        if not cls.__table_name__:
            raise TypeError(f"{cls.__name__} is not mapped to a table (no __table_name__)")
        return f"{cls.__schema__}.{cls.__table_name__}"

    @classmethod
    def column_names(cls) -> list[str]:
        return list(cls.model_fields)
