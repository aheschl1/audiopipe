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
from api.schema.locations import LocationPointRead, SessionTrackRead, TrackPointRead
from database.repos.locations import TrackPointRow

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


@router.get("/location/tracks", summary="Per-session GPS tracks by wall-clock time")
async def list_location_tracks(
    user: CurrentUser,
    pipe: Pipe,
    window: Range,
    max_points: int = Query(
        500,
        ge=2,
        le=2000,
        description="Decimation cap per session; the first and last fixes always survive.",
    ),
) -> list[SessionTrackRead]:
    """Your sessions' tracks as decimated polylines, one entry per session.

    The window is epoch milliseconds, half-open on ``captured_at``, exactly
    like the sibling points route — but instead of paging raw fixes this
    thins each session's in-window points to an even stride of at most
    ``max_points`` (+1: the endpoint fix is kept), while ``point_count`` and
    the lat/lon bounds stay exact. Only sessions with a fix in the window
    appear; sessions come back oldest first. A windowless call scans every
    stored batch you own.
    """
    rows = await pipe.locations.track_points_for_user(
        user.id,
        from_at=_at(window.from_ms),
        to_at=_at(window.to_ms),
        max_points=max_points,
    )
    tracks: list[SessionTrackRead] = []
    run: list[TrackPointRow] = []
    for row in rows:
        if run and row.session_id != run[0].session_id:
            tracks.append(_track(run))
            run = []
        run.append(row)
    if run:
        tracks.append(_track(run))
    return tracks


def _track(rows: list[TrackPointRow]) -> SessionTrackRead:
    head = rows[0]
    return SessionTrackRead(
        session_id=head.session_id,
        label=head.label,
        device=head.device,
        started_at=head.started_at,
        point_count=head.total_points,
        min_lat=head.min_lat,
        max_lat=head.max_lat,
        min_lon=head.min_lon,
        max_lon=head.max_lon,
        points=[TrackPointRead.from_model(row) for row in rows],
    )


def _at(epoch_ms: int | None) -> datetime | None:
    return None if epoch_ms is None else datetime.fromtimestamp(epoch_ms / 1000, tz=UTC)
