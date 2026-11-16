"""Discovery for the per-segment visual pipelines (``thumbnail``, ``video-proxy``)."""

from database.repos.base import BaseRepo
from database.repos.segments import COLUMNS as _SEGMENT_COLUMNS
from database.schema.segments import ResourceSegment

_S_COLUMNS = ", ".join(f"s.{column.strip()}" for column in _SEGMENT_COLUMNS.split(","))


def segment_dedup_key(pipeline: str, segment_id: object) -> str:
    return f"{pipeline}:segment:{segment_id}"


class VisualQueries(BaseRepo):
    async def segments_without_jobs(
        self, pipeline: str, resources: tuple[str, ...], limit: int = 100
    ) -> list[ResourceSegment]:
        """Blob segments of ``resources`` this pipeline has never been enqueued for.

        Per segment, not per session: visual segments are whole files, so they
        are work as soon as they land, open session or not. The anti-join rides
        the ``(pipeline, dedup_key)`` unique index.
        """
        return await self._fetch_all(
            ResourceSegment,
            f"""
                SELECT {_S_COLUMNS} FROM resource_segments s
                WHERE s.resource = ANY(%s) AND s.object_key IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM processing_jobs j
                      WHERE j.pipeline = %s
                        AND j.dedup_key = %s || ':segment:' || s.id::text
                  )
                ORDER BY s.ingested_at, s.id
                LIMIT %s
            """,
            (list(resources), pipeline, pipeline, limit),
        )
