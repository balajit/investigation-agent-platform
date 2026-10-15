# src/investigation_agent_platform/api/v1/schemas/envelopes.py
"""Shared response envelopes. Shapes match what handlers emit today."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ListEnvelope[T](BaseModel):
    """`{items, total[, offset, limit]}` — paginated and non-paginated lists."""

    items: list[T] = Field(description="Page of results.")
    total: int = Field(ge=0, description="Total rows available, not just this page.")
    offset: int | None = Field(
        default=None,
        ge=0,
        description="Offset echo. Present only for endpoints that paginate.",
    )
    limit: int | None = Field(
        default=None,
        ge=1,
        description="Limit echo. Present only for endpoints that paginate.",
    )


class ItemsEnvelope[T](BaseModel):
    """`{items}` — a bare list with no total (e.g. reference-docs/sources)."""

    items: list[T] = Field(description="All results.")


class CursorEnvelope[T](BaseModel):
    """`{items, next_cursor, has_more}` — opaque cursor pagination."""

    items: list[T] = Field(description="Page of results.")
    next_cursor: str | None = Field(
        default=None,
        description="Opaque continuation token. Explicit null on the last page.",
    )
    has_more: bool = Field(
        default=False,
        description="True when `next_cursor` is non-null.",
    )
