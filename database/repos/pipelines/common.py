"""Discovery queries shared by more than one pipeline.

Each pipeline still owns *what* it discovers; this module holds the recurring
shapes so they are written (and indexed) once.
"""

from database.repos.base import BaseRepo
from database.schema.sessions import RecordingSession

_SESSION_COLUMNS = ", ".join(
    f"s.{column}"
    for column in (
        "id, user_id, device, label, started_at, ended_at, "
        "metadata, created_at, updated_at"
    ).split(", ")
)


class PipelineDiscovery(BaseRepo):
    async def ended_sessions_without(
        self, pipeline: str, limit: int = 100
    ) -> list[RecordingSession]:
        """Ended sessions ``pipeline`` has never been enqueued for.

        The NOT EXISTS only keeps discovery cheap; the dedup unique index on
        processing_jobs is the correctness backstop if two passes race. It
        anti-joins on ``(pipeline, session_id)`` — indexed on both sides —
        rather than reconstructing a dedup key per row, which no index can
        serve. (Any job of this pipeline for the session suppresses discovery,
        which for a one-job-per-session pipeline is the same set.)
        """
        return await self._fetch_all(
            RecordingSession,
            f"""
                SELECT {_SESSION_COLUMNS} FROM recording_sessions s
                WHERE s.ended_at IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM processing_jobs j
                      WHERE j.pipeline = %s AND j.session_id = s.id
                  )
                ORDER BY s.ended_at
                LIMIT %s
            """,
            (pipeline, limit),
        )

    async def ended_sessions_with_resource_without(
        self, pipeline: str, resource: str, limit: int = 100
    ) -> list[RecordingSession]:
        """:meth:`ended_sessions_without`, restricted to sessions that hold
        at least one segment of ``resource``.

        The shape resource-typed pipelines discover with: a session that never
        captured their resource is not their work, and gets no job (and hence
        no empty completion marker). The EXISTS probe rides the
        (session_id, resource, ...) index.
        """
        return await self._fetch_all(
            RecordingSession,
            f"""
                SELECT {_SESSION_COLUMNS} FROM recording_sessions s
                WHERE s.ended_at IS NOT NULL
                  AND EXISTS (
                      SELECT 1 FROM resource_segments rs
                      WHERE rs.session_id = s.id AND rs.resource = %s
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM processing_jobs j
                      WHERE j.pipeline = %s AND j.session_id = s.id
                  )
                ORDER BY s.ended_at
                LIMIT %s
            """,
            (resource, pipeline, limit),
        )
