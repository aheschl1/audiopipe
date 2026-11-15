"""Raw-SQL repositories for the pgvector tables (``speaker_embeddings``,
``audio_embeddings``, ``visual_embeddings``).

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
    VisualEmbeddingCreate,
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
        limit: int = 20,
    ) -> list[AudioSearchHit]:
        """Windows nearest a CLAP text embedding, closest first — the
        retrieval primitive behind text->audio search. Scoped to one user's
        sessions (embeddings inherit session ownership); an exact scan is fine
        at current volumes — see the ANN note in migration 0008."""
        conditions = ["s.user_id = %s"]
        params: list = [_literal(vector), user_id]
        if session_id is not None:
            conditions.append("e.session_id = %s")
            params.append(session_id)
        params += [_literal(vector), limit]
        return await self._fetch_all(
            AudioSearchHit,
            f"""
                SELECT e.artifact_id, e.session_id, a.start_ms, a.end_ms,
                       e.embedding <=> %s::vector AS distance,
                       a.metadata, e.created_at
                FROM audio_embeddings e
                JOIN pipeline_artifacts a ON a.id = e.artifact_id
                JOIN recording_sessions s ON s.id = e.session_id
                WHERE {" AND ".join(conditions)}
                ORDER BY e.embedding <=> %s::vector
                LIMIT %s
            """,
            params,
        )


class FrameSearchHit(BaseModel):
    model_config = ConfigDict(frozen=True)

    artifact_id: UUID
    session_id: UUID
    start_ms: int | None
    distance: float
    links: dict[str, Any]
    metadata: dict[str, Any]


class VisualEmbeddingsRepo(BaseRepo):
    async def create_many(self, items: Sequence[VisualEmbeddingCreate]) -> int:
        if not items:
            return 0
        params: list = []
        for item in items:
            params += [item.artifact_id, item.session_id, item.model, _literal(item.embedding)]
        values = ", ".join(["(%s, %s, %s, %s::vector)"] * len(items))
        return await self._execute(
            f"""
                INSERT INTO visual_embeddings (artifact_id, session_id, model, embedding)
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
        limit: int = 20,
    ) -> list[FrameSearchHit]:
        """Keyframes nearest a SigLIP text embedding, closest first (exact scan)."""
        conditions = ["s.user_id = %s"]
        params: list = [_literal(vector), user_id]
        if session_id is not None:
            conditions.append("e.session_id = %s")
            params.append(session_id)
        params += [_literal(vector), limit]
        return await self._fetch_all(
            FrameSearchHit,
            f"""
                SELECT e.artifact_id, e.session_id, a.start_ms,
                       e.embedding <=> %s::vector AS distance, a.links, a.metadata
                FROM visual_embeddings e
                JOIN pipeline_artifacts a ON a.id = e.artifact_id
                JOIN recording_sessions s ON s.id = e.session_id
                WHERE {" AND ".join(conditions)}
                ORDER BY e.embedding <=> %s::vector
                LIMIT %s
            """,
            params,
        )
