"""Response shapes shared by every route."""

from typing import Annotated, Sequence, Type, TypeVar

from fastapi import Query
from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")

MAX_LIMIT = 200


class PageParams(BaseModel):
    """`limit` / `offset` query parameters for every list endpoint."""

    model_config = ConfigDict(frozen=True)

    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 50
    offset: Annotated[int, Query(ge=0)] = 0

    @property
    def probe_limit(self) -> int:
        """One row more than asked for; ``Page.build`` trims it into ``has_more``."""
        return self.limit + 1


class RangeParams(BaseModel):
    """`from_ms` / `to_ms` query parameters for time-windowed listings.

    The window is half-open ``[from_ms, to_ms)``, so adjacent windows
    partition a stream — no duplicates, no gaps. Session-scoped routes read
    it as session-relative milliseconds; cross-session routes as epoch
    milliseconds (the same unit resource payloads carry).
    """

    model_config = ConfigDict(frozen=True)

    from_ms: Annotated[
        int | None, Query(ge=0, description="Only items at/after this time, ms.")
    ] = None
    to_ms: Annotated[
        int | None, Query(ge=0, description="Only items before this time, ms.")
    ] = None


class Page[T](BaseModel):
    """A slice of a listing. Carries no total count -- see ``has_more``."""

    model_config = ConfigDict(frozen=True)

    items: list[T]
    limit: int
    offset: int
    has_more: bool = Field(description="True when more rows exist past this slice.")

    @classmethod
    def build(cls, rows: Sequence, params: PageParams, model) -> "Page[T]":
        """Trim the extra probe row and map the rest into response models."""
        has_more = len(rows) > params.limit
        visible = rows[: params.limit]
        return cls(
            items=[model(row) for row in visible],
            limit=params.limit,
            offset=params.offset,
            has_more=has_more,
        )


class ErrorResponse(BaseModel):
    """The body of every non-2xx response."""

    detail: str
