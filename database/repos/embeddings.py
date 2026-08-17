"""Raw-SQL repositories for the pgvector tables (``speaker_embeddings``,
``audio_embeddings``).

Vectors cross the wire in pgvector's text form (``[1.0,2.0]``) with an
explicit ``::vector`` cast, so no driver-side type registration is needed.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from database.repos.base import BaseRepo
from database.schema.embeddings import (
    AudioEmbeddingCreate,
    SpeakerEmbedding,
    SpeakerEmbeddingCreate,
)

# embedding reads back as text: psycopg has no codec for the vector OID.
COLUMNS = "artifact_id, session_id, speaker, model, embedding::text AS embedding, created_at"


def _literal(vector: Sequence[float]) -> str:
    return "[" + ",".join(str(value) for value in vector) + "]"


class EmbeddingsRepo(BaseRepo):
    async def create_many(self, items: Sequence[SpeakerEmbeddingCreate]) -> int:
        """Insert a batch in one statement; rows ride the caller's transaction
        alongside the artifact rows they reference."""
        if not items:
            return 0
        params: list = []
        for item in items:
            params += [
                item.artifact_id,
                item.session_id,
                item.speaker,
                item.model,
                _literal(item.embedding),
            ]
        values = ", ".join(["(%s, %s, %s, %s, %s::vector)"] * len(items))
        return await self._execute(
            f"""
                INSERT INTO speaker_embeddings
                    (artifact_id, session_id, speaker, model, embedding)
                VALUES {values}
            """,
            params,
        )

    async def list_for_session(self, session_id: UUID) -> list[SpeakerEmbedding]:
        return await self._fetch_all(
            SpeakerEmbedding,
            f"""
                SELECT {COLUMNS} FROM speaker_embeddings
                WHERE session_id = %s
                ORDER BY speaker, artifact_id
            """,
            (session_id,),
        )

    async def nearest(
        self, vector: Sequence[float], *, limit: int = 5
    ) -> list[SpeakerEmbedding]:
        """Closest stored embeddings by cosine distance — the primitive the
        clustering tier attaches new block-speakers with."""
        return await self._fetch_all(
            SpeakerEmbedding,
            f"""
                SELECT {COLUMNS} FROM speaker_embeddings
                ORDER BY embedding <=> %s::vector
                LIMIT %s
            """,
            (_literal(vector), limit),
        )


class AudioSearchHit(BaseModel):
    """One window matching a text query, with its timeline addressing and the
    tag metadata its artifact carries (``labels``, ``model``)."""

    model_config = ConfigDict(frozen=True)

    artifact_id: UUID
    session_id: UUID
    start_ms: int
    end_ms: int
    distance: float
    metadata: dict[str, Any]
    created_at: datetime
    occurred_at: datetime | None = None  # wall-clock moment of start_ms
    session_label: str | None = None


class AudioEmbeddingsRepo(BaseRepo):
    async def create_many(self, items: Sequence[AudioEmbeddingCreate]) -> int:
        """Insert a batch in one statement; rows ride the caller's transaction
        alongside the artifact rows they reference."""
        if not items:
            return 0
        params: list = []
        for item in items:
            params += [
                item.artifact_id,
                item.session_id,
                item.model,
                _literal(item.embedding),
            ]
        values = ", ".join(["(%s, %s, %s, %s::vector)"] * len(items))
        return await self._execute(
            f"""
                INSERT INTO audio_embeddings
                    (artifact_id, session_id, model, embedding)
                VALUES {values}
            """,
            params,
        )

    async def search(
        self,
        vector: Sequence[float],
        *,
        user_id: UUID,
        session_id: UUID | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 20,
    ) -> list[AudioSearchHit]:
        """Windows nearest a CLAP text embedding, closest first — the
        retrieval primitive behind text->audio search. Scoped to one user's
        sessions (embeddings inherit session ownership); an exact scan is fine
        at current volumes — see the ANN note in migration 0008.
        ``since``/``until`` bound the window's wall-clock moment."""
        conditions = ["s.user_id = %s"]
        params: list = [_literal(vector), user_id]
        if session_id is not None:
            conditions.append("e.session_id = %s")
            params.append(session_id)
        if since is not None:
            conditions.append("(s.ended_at IS NULL OR s.ended_at > %s)")
            params.append(since)
            conditions.append("s.started_at + make_interval(secs => a.end_ms / 1000.0) > %s")
            params.append(since)
        if until is not None:
            conditions.append("s.started_at < %s")
            params.append(until)
            conditions.append("s.started_at + make_interval(secs => a.start_ms / 1000.0) < %s")
            params.append(until)
        params += [_literal(vector), limit]
        return await self._fetch_all(
            AudioSearchHit,
            f"""
                SELECT e.artifact_id, e.session_id, a.start_ms, a.end_ms,
                       e.embedding <=> %s::vector AS distance,
                       a.metadata, e.created_at,
                       s.started_at + make_interval(secs => a.start_ms / 1000.0)
                           AS occurred_at,
                       s.label AS session_label
                FROM audio_embeddings e
                JOIN pipeline_artifacts a ON a.id = e.artifact_id
                JOIN recording_sessions s ON s.id = e.session_id
                WHERE {" AND ".join(conditions)}
                ORDER BY e.embedding <=> %s::vector
                LIMIT %s
            """,
            params,
        )
