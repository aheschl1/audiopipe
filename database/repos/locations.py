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
