"""Discovery and aggregation queries for the ``session-stats`` pipeline."""

from uuid import UUID

from pydantic import BaseModel

from database.repos.pipelines.common import PipelineDiscovery


class ResourceSegmentAggregates(BaseModel):
    """One resource's per-session totals, computed in SQL rather than by
    loading every row."""

    resource: str
    segments: int
    total_bytes: int
    duration_ms: int
    segment_ids: list[UUID]


class SessionStatsQueries(PipelineDiscovery):
    async def aggregate_segments(
        self, session_id: UUID
    ) -> list[ResourceSegmentAggregates]:
        """One aggregate row per resource instead of every segment across the
        wire.

        A day-long chunk-per-second session is 86 400 rows; the stats need
        three sums and the id list, so let Postgres do the arithmetic. A
        resource the session never captured has no row.
        """
        return await self._fetch_all(
            ResourceSegmentAggregates,
            """
                SELECT resource,
                       COUNT(*) AS segments,
                       COALESCE(SUM(byte_size), 0) AS total_bytes,
                       COALESCE(SUM(duration_ms), 0) AS duration_ms,
                       COALESCE(ARRAY_AGG(id ORDER BY sequence), ARRAY[]::uuid[])
                           AS segment_ids
                FROM resource_segments WHERE session_id = %s
                GROUP BY resource ORDER BY resource
            """,
            (session_id,),
        )
