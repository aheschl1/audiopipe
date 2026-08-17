"""Point-level queries over inline location batches.

Location segments store one JSON batch per row (``payload->'points'``, each
``{lat, lon, t, alt_m?, accuracy_m?}`` with ``t`` in epoch ms — normalized by
the resource's ingest validator). These queries unnest the batches in SQL and
paginate at the *point* level, so a caller never re-expands rows in Python.

Row-level prefilters lean on the ingest contract that ``captured_at`` is the
first point's time and ``duration_ms`` the batch span, and (cross-session) on
the partial ``(user_id, captured_at) WHERE resource='location'`` index.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from database.repos.base import BaseRepo

# The SELECT list shared by both queries; at_ms is the point's position on its
# session's timeline (wall clock minus session start — may drift from audio
# byte-time on gappy captures, and may be negative for a fix taken just
# before the session row was created).
_POINT_COLUMNS = """
    s.session_id,
    s.id AS segment_id,
    ((p.pt->>'t')::bigint
        - (EXTRACT(EPOCH FROM rs.started_at) * 1000)::bigint) AS at_ms,
    to_timestamp((p.pt->>'t')::bigint / 1000.0) AS captured_at,
    (p.pt->>'lat')::float8 AS lat,
    (p.pt->>'lon')::float8 AS lon,
    (p.pt->>'alt_m')::float8 AS alt_m,
    (p.pt->>'accuracy_m')::float8 AS accuracy_m,
    p.idx
"""

_POINT_SOURCE = """
    FROM resource_segments s
    JOIN recording_sessions rs ON rs.id = s.session_id
    CROSS JOIN LATERAL
        jsonb_array_elements(s.payload->'points') WITH ORDINALITY AS p(pt, idx)
"""


class LocationPointRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: UUID
    segment_id: UUID
    at_ms: int
    captured_at: datetime
    lat: float
    lon: float
    alt_m: float | None = None
    accuracy_m: float | None = None


class TrackPointRow(BaseModel):
    """One decimated track point plus its session's window-wide aggregates.

    ``total_points`` and the bounds describe every in-window point of the
    session, computed before decimation, so the caller gets exact figures no
    matter how few points survive the stride.
    """

    model_config = ConfigDict(frozen=True)

    session_id: UUID
    label: str | None = None
    device: str | None = None
    started_at: datetime
    at_ms: int
    captured_at: datetime
    lat: float
    lon: float
    total_points: int
    min_lat: float
    max_lat: float
    min_lon: float
    max_lon: float
    first_at: datetime


class LocationsRepo(BaseRepo):
    async def points_for_session(
        self,
        session_id: UUID,
        from_ms: int | None = None,
        to_ms: int | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[LocationPointRow]:
        """A session's points in timeline order, half-open ``[from_ms, to_ms)``.

        The half-open window matches the timeline's event filtering, so
        adjacent windows partition the stream without duplicates.
        """
        filters = ""
        params: list[object] = [session_id]
        if from_ms is not None:
            filters += " AND q.at_ms >= %s"
            params.append(from_ms)
        if to_ms is not None:
            filters += " AND q.at_ms < %s"
            params.append(to_ms)
        params += [limit, offset]
        return await self._fetch_all(
            LocationPointRow,
            f"""
                SELECT * FROM (
                    SELECT {_POINT_COLUMNS}
                    {_POINT_SOURCE}
                    WHERE s.session_id = %s AND s.resource = 'location'
                ) q
                WHERE TRUE {filters}
                ORDER BY q.at_ms, q.segment_id, q.idx
                LIMIT %s OFFSET %s
            """,
            tuple(params),
        )

    async def points_for_user(
        self,
        user_id: UUID,
        from_at: datetime | None = None,
        to_at: datetime | None = None,
        session_id: UUID | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[LocationPointRow]:
        """Every point of one user across sessions, by wall clock,
        half-open ``[from_at, to_at)``, oldest first.

        Rows are prefiltered on their batch span (``captured_at`` +
        ``duration_ms``) before the lateral unnest, so only candidate batches
        are expanded; the point-level filter then trims the batch edges.
        """
        row_filters = ""
        params: list[object] = [user_id]
        if session_id is not None:
            row_filters += " AND s.session_id = %s"
            params.append(session_id)
        if from_at is not None:
            row_filters += (
                " AND s.captured_at"
                "     + make_interval(secs => COALESCE(s.duration_ms, 0) / 1000.0) >= %s"
            )
            params.append(from_at)
        if to_at is not None:
            row_filters += " AND s.captured_at < %s"
            params.append(to_at)
        point_filters = ""
        if from_at is not None:
            point_filters += " AND q.captured_at >= %s"
            params.append(from_at)
        if to_at is not None:
            point_filters += " AND q.captured_at < %s"
            params.append(to_at)
        params += [limit, offset]
        return await self._fetch_all(
            LocationPointRow,
            f"""
                SELECT * FROM (
                    SELECT {_POINT_COLUMNS}
                    {_POINT_SOURCE}
                    WHERE s.user_id = %s AND s.resource = 'location' {row_filters}
                ) q
                WHERE TRUE {point_filters}
                ORDER BY q.captured_at, q.segment_id, q.idx
                LIMIT %s OFFSET %s
            """,
            tuple(params),
        )

    async def track_points_for_user(
        self,
        user_id: UUID,
        from_at: datetime,
        to_at: datetime,
        max_points: int = 500,
    ) -> list[TrackPointRow]:
        """Per-session decimated tracks by wall clock, half-open ``[from_at, to_at)``.

        Points are thinned to an even stride so no session returns more than
        ``max_points`` (+1: the last fix always survives, so a track never
        loses its endpoint). Aggregates ride along on every row computed over
        the *full* in-window point set. Sessions come back contiguous, oldest
        first by their first in-window fix. The window is required: unlike
        the paged siblings there is no LIMIT, so the window is the only
        bound on how many points get unnested and aggregated.
        """
        return await self._fetch_all(
            TrackPointRow,
            f"""
                SELECT t.session_id, t.label, t.device, t.started_at,
                       t.at_ms, t.captured_at, t.lat, t.lon,
                       t.total_points, t.min_lat, t.max_lat, t.min_lon, t.max_lon,
                       t.first_at
                FROM (
                    SELECT q.*,
                        row_number() OVER w AS rn,
                        count(*) OVER p AS total_points,
                        min(q.lat) OVER p AS min_lat,
                        max(q.lat) OVER p AS max_lat,
                        min(q.lon) OVER p AS min_lon,
                        max(q.lon) OVER p AS max_lon,
                        min(q.captured_at) OVER p AS first_at
                    FROM (
                        SELECT {_POINT_COLUMNS},
                            rs.label, rs.device, rs.started_at
                        {_POINT_SOURCE}
                        WHERE s.user_id = %s AND s.resource = 'location'
                          AND s.captured_at
                              + make_interval(secs => COALESCE(s.duration_ms, 0) / 1000.0) >= %s
                          AND s.captured_at < %s
                    ) q
                    WHERE q.captured_at >= %s AND q.captured_at < %s
                    WINDOW p AS (PARTITION BY q.session_id),
                           w AS (PARTITION BY q.session_id
                                 ORDER BY q.captured_at, q.segment_id, q.idx)
                ) t
                WHERE (t.rn - 1) %% GREATEST(1, (t.total_points + %s - 1) / %s) = 0
                   OR t.rn = t.total_points
                ORDER BY t.first_at, t.session_id, t.rn
            """,
            (user_id, from_at, to_at, from_at, to_at, max_points, max_points),
        )
