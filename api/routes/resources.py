"""Cross-session resource queries.

The ``/v1/resources/{resource}/...`` namespace queries one resource across
every session the caller owns — the first (and so far only) member is the
wall-clock location query. Session-scoped resource routes live under
``/v1/sessions/{id}/resources/...`` instead.
"""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Query

from api.deps import CurrentUser, Paging, Pipe, Range
from api.schema.common import Page
from api.schema.locations import LocationPointRead

router = APIRouter(prefix="/resources", tags=["resources"])


@router.get("/location/points", summary="Query your location by wall-clock time")
async def list_location_points(
    user: CurrentUser,
    pipe: Pipe,
    paging: Paging,
    window: Range,
    session_id: UUID | None = Query(
        None, description="Only points of this session (foreign ids match nothing)."
    ),
) -> Page[LocationPointRead]:
    """Every stored fix across your sessions, oldest first.

    The window is the same `from_ms`/`to_ms` range every timeline route
    takes, read as **epoch milliseconds** here — there is no single session
    to be relative to, and it is the unit location payloads already carry in
    ``t``. Half-open on the fix's ``captured_at``. Like the search routes,
    ``session_id`` narrows within your own data rather than 404ing on
    someone else's id.
    """
    rows = await pipe.locations.points_for_user(
        user.id,
        from_at=_at(window.from_ms),
        to_at=_at(window.to_ms),
        session_id=session_id,
        limit=paging.probe_limit,
        offset=paging.offset,
    )
    return Page.build(rows, paging, LocationPointRead.from_model)


def _at(epoch_ms: int | None) -> datetime | None:
    return None if epoch_ms is None else datetime.fromtimestamp(epoch_ms / 1000, tz=UTC)
