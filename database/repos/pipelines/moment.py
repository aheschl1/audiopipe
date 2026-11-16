"""Inputs for the ``moment`` pipeline: wall-clock activity, settledness and items."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from database.repos.base import BaseRepo

# Only these resources make a moment; location pings would chain a whole day.
class ActivityRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: UUID
    session_id: UUID
    start_at: datetime
    end_at: datetime
    open: bool  # an audio session that has not ended
    last_ingested_at: datetime


class SessionRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: UUID


class KeyRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    dedup_key: str


class SessionDigest(BaseModel):
    """What a moment was built from, per session; a change triggers a rebuild."""

    model_config = ConfigDict(frozen=True)

    session_id: UUID
    segments: int
    artifacts: int
    last_artifact_at: datetime | None


class ItemRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    session_id: UUID
    pipeline: str
    kind: str
    start_ms: int
    end_ms: int | None
    started_at: datetime
    object_key: str | None
    links: dict[str, Any]
    metadata: dict[str, Any]


class SpeakerNameRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: UUID
    speaker: str
    speaker_id: UUID | None
    name: str | None


class MomentQueries(BaseRepo):
    async def activity(self, lookback_days: int) -> list[ActivityRow]:
        """Wall-clock spans of recording: one per audio session, one per keyframe.

        Everything is placed by timeline time (session start plus offset), so a
        photo or clip goes where its keyframes sit; unplaced frames don't count.
        """
        return await self._fetch_all(
            ActivityRow,
            """
                SELECT s.user_id, s.id AS session_id,
                       s.started_at + make_interval(
                           secs => coalesce(min(g.offset_ms), 0) / 1000.0) AS start_at,
                       -- Segments without an offset are laid end to end.
                       s.started_at + make_interval(secs => greatest(
                           max(g.offset_ms + coalesce(g.duration_ms, 0)),
                           sum(coalesce(g.duration_ms, 0))) / 1000.0) AS end_at,
                       s.ended_at IS NULL AS open,
                       max(g.ingested_at) AS last_ingested_at
                FROM recording_sessions s
                JOIN resource_segments g ON g.session_id = s.id
                WHERE g.resource = 'audio'
                  AND s.started_at > now() - make_interval(days => %s)
                GROUP BY s.id
                UNION ALL
                SELECT s.user_id, a.session_id,
                       s.started_at + make_interval(secs => a.start_ms / 1000.0),
                       s.started_at + make_interval(
                           secs => coalesce(a.end_ms, a.start_ms) / 1000.0),
                       s.ended_at IS NULL, a.created_at
                FROM pipeline_artifacts a
                JOIN recording_sessions s ON s.id = a.session_id
                WHERE a.pipeline = 'keyframe' AND a.kind = 'keyframe'
                  AND a.start_ms IS NOT NULL
                  AND s.started_at > now() - make_interval(days => %s)
            """,
            (lookback_days, lookback_days),
        )

    async def existing_keys(self, pipeline: str, keys: list[str]) -> set[str]:
        """The dedup keys among ``keys`` that already have a job."""
        rows = await self._fetch_all(
            KeyRow,
            "SELECT dedup_key FROM processing_jobs WHERE pipeline = %s AND dedup_key = ANY(%s)",
            (pipeline, keys),
        )
        return {row.dedup_key for row in rows}

    async def busy_sessions(self, session_ids: list[UUID], pipeline: str) -> set[UUID]:
        """Sessions with work still queued or running in any other pipeline."""
        rows = await self._fetch_all(
            SessionRow,
            """
                SELECT DISTINCT session_id FROM processing_jobs
                WHERE session_id = ANY(%s) AND pipeline <> %s
                  AND status IN ('queued', 'running')
            """,
            (session_ids, pipeline),
        )
        return {row.session_id for row in rows}

    async def digests(self, session_ids: list[UUID]) -> list[SessionDigest]:
        return await self._fetch_all(
            SessionDigest,
            """
                SELECT s.id AS session_id,
                       (SELECT count(*) FROM resource_segments g WHERE g.session_id = s.id) AS segments,
                       count(a.id) AS artifacts,
                       max(a.created_at) AS last_artifact_at
                FROM recording_sessions s
                LEFT JOIN pipeline_artifacts a ON a.session_id = s.id
                  AND ((a.pipeline = 'transcribe' AND a.kind = 'transcript')
                    OR (a.pipeline = 'keyframe' AND a.kind = 'keyframe')
                    OR (a.pipeline = 'sound-span' AND a.kind = 'sound-span'))
                WHERE s.id = ANY(%s)
                GROUP BY s.id
                ORDER BY s.id
            """,
            (session_ids,),
        )

    async def items(self, session_ids: list[UUID], start: datetime, end: datetime) -> list[ItemRow]:
        """Transcripts, keyframes and sound spans of these sessions placed in [start, end]."""
        return await self._fetch_all(
            ItemRow,
            """
                SELECT a.id, a.session_id, a.pipeline, a.kind, a.start_ms, a.end_ms,
                       s.started_at, a.object_key, a.links, a.metadata
                FROM pipeline_artifacts a
                JOIN recording_sessions s ON s.id = a.session_id
                WHERE a.session_id = ANY(%s) AND a.start_ms IS NOT NULL
                  AND ((a.pipeline = 'transcribe' AND a.kind = 'transcript')
                    OR (a.pipeline = 'keyframe' AND a.kind = 'keyframe')
                    OR (a.pipeline = 'sound-span' AND a.kind = 'sound-span'))
                  AND s.started_at + make_interval(secs => a.start_ms / 1000.0) <= %s
                  AND s.started_at + make_interval(
                      secs => coalesce(a.end_ms, a.start_ms) / 1000.0) >= %s
                ORDER BY s.started_at + make_interval(secs => a.start_ms / 1000.0), a.id
            """,
            (session_ids, end, start),
        )

    async def speaker_names(self, session_ids: list[UUID]) -> list[SpeakerNameRow]:
        return await self._fetch_all(
            SpeakerNameRow,
            """
                SELECT e.session_id, e.speaker, e.speaker_id, s.name
                FROM speaker_embeddings e
                LEFT JOIN speakers s ON s.id = e.speaker_id
                WHERE e.session_id = ANY(%s)
            """,
            (session_ids,),
        )
