"""Raw-SQL repository for the ``pipeline_artifacts`` table."""

from collections.abc import Sequence
from uuid import UUID

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from database.repos.base import BaseRepo
from database.schema.artifacts import ArtifactCreate, PipelineArtifact

COLUMNS = (
    "id, pipeline, session_id, kind, start_ms, end_ms, bucket, object_key, "
    "links, metadata, created_at, updated_at"
)


class ArtifactsRepo(BaseRepo):
    async def create(self, data: ArtifactCreate) -> PipelineArtifact:
        artifact = await self._fetch_one(
            PipelineArtifact,
            f"""
                INSERT INTO pipeline_artifacts
                    (pipeline, kind, session_id, start_ms, end_ms,
                     bucket, object_key, links, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING {COLUMNS}
            """,
            (
                data.pipeline,
                data.kind,
                data.session_id,
                data.start_ms,
                data.end_ms,
                data.bucket,
                data.object_key,
                Jsonb(data.links),
                Jsonb(data.metadata),
            ),
        )
        assert artifact is not None  # INSERT ... RETURNING always yields a row
        return artifact

    async def create_many(self, items: Sequence[ArtifactCreate]) -> list[PipelineArtifact]:
        """Insert a batch in one statement, in order.

        The publication path for pipelines that emit many artifacts per run
        (diarize: hundreds of turns) — one round trip, one transaction with
        whatever else the caller is publishing.
        """
        if not items:
            return []
        params: list = []
        for item in items:
            params += [
                item.pipeline,
                item.kind,
                item.session_id,
                item.start_ms,
                item.end_ms,
                item.bucket,
                item.object_key,
                Jsonb(item.links),
                Jsonb(item.metadata),
            ]
        values = ", ".join(["(%s, %s, %s, %s, %s, %s, %s, %s, %s)"] * len(items))
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                    INSERT INTO pipeline_artifacts
                        (pipeline, kind, session_id, start_ms, end_ms,
                         bucket, object_key, links, metadata)
                    VALUES {values}
                    RETURNING {COLUMNS}
                """,
                params,
            )
            rows = await cur.fetchall()
        return [PipelineArtifact.model_validate(row) for row in rows]

    async def delete_for_pipeline(self, session_id: UUID, pipeline: str) -> int:
        """Remove every artifact one pipeline produced for a session.

        The idempotent-republication primitive: a retrying job clears its own
        previous output before writing the new set, in one transaction, so
        consumers never observe a partial mix. Blobs are untouched (rewriting
        jobs overwrite the same keys).
        """
        return await self._execute(
            "DELETE FROM pipeline_artifacts WHERE session_id = %s AND pipeline = %s",
            (session_id, pipeline),
        )

    async def delete_for_segment(self, session_id: UUID, pipeline: str, segment_id: UUID) -> int:
        """Like :meth:`delete_for_pipeline`, for tiers whose jobs are per segment."""
        return await self._execute(
            """
                DELETE FROM pipeline_artifacts
                WHERE session_id = %s AND pipeline = %s AND links->>'segment' = %s
            """,
            (session_id, pipeline, str(segment_id)),
        )

    async def get(self, artifact_id: UUID) -> PipelineArtifact | None:
        return await self._fetch_one(
            PipelineArtifact,
            f"SELECT {COLUMNS} FROM pipeline_artifacts WHERE id = %s",
            (artifact_id,),
        )

    async def find_by_link(
        self, pipeline: str, kind: str, key: str, target: str
    ) -> PipelineArtifact | None:
        """The newest matching artifact linking ``target`` under ``key``."""
        return await self._fetch_one(
            PipelineArtifact,
            f"""
                SELECT {COLUMNS} FROM pipeline_artifacts
                WHERE pipeline = %s AND kind = %s AND links->>%s = %s
                ORDER BY created_at DESC LIMIT 1
            """,
            (pipeline, kind, key, target),
        )

    async def merge_metadata(
        self, artifact_id: UUID, patch: dict, *, drop: Sequence[str] = ()
    ) -> PipelineArtifact | None:
        """Merge ``patch`` into the artifact's metadata (jsonb ``||``, so
        untouched keys survive) — the user-curation path, e.g. transcript
        edits. ``drop`` removes keys first: an edit invalidates derived data
        like word timings. Pipelines republish whole artifacts instead."""
        return await self._fetch_one(
            PipelineArtifact,
            f"""
                UPDATE pipeline_artifacts
                SET metadata = (metadata - %s::text[]) || %s
                WHERE id = %s
                RETURNING {COLUMNS}
            """,
            (list(drop), Jsonb(patch), artifact_id),
        )

    async def find(
        self, pipeline: str, kind: str, session_id: UUID
    ) -> PipelineArtifact | None:
        """The newest matching artifact — how a consumer locates an upstream
        pipeline's output for a session."""
        return await self._fetch_one(
            PipelineArtifact,
            f"""
                SELECT {COLUMNS} FROM pipeline_artifacts
                WHERE pipeline = %s AND kind = %s AND session_id = %s
                ORDER BY created_at DESC, id DESC
                LIMIT 1
            """,
            (pipeline, kind, session_id),
        )

    async def find_overlapping(
        self,
        session_id: UUID,
        from_ms: int,
        to_ms: int,
        pipeline: str | None = None,
        kind: str | None = None,
        limit: int = 100,
    ) -> list[PipelineArtifact]:
        """Span artifacts touching ``[from_ms, to_ms)``, in timeline order.

        Whole-session artifacts (NULL span) are excluded — this is the
        "what is attached to this chunk of the session" query.
        """
        sql = f"""
            SELECT {COLUMNS} FROM pipeline_artifacts
            WHERE session_id = %s AND start_ms IS NOT NULL
              AND start_ms < %s AND end_ms > %s
        """
        params: list = [session_id, to_ms, from_ms]
        if pipeline is not None:
            sql += " AND pipeline = %s"
            params.append(pipeline)
        if kind is not None:
            sql += " AND kind = %s"
            params.append(kind)
        sql += " ORDER BY start_ms, id LIMIT %s"
        params.append(limit)
        return await self._fetch_all(PipelineArtifact, sql, params)

    async def list_for_session(
        self,
        session_id: UUID,
        pipeline: str | None = None,
        kind: str | None = None,
        from_ms: int | None = None,
        to_ms: int | None = None,
        limit: int = 50,
        offset: int = 0,
        pairs: Sequence[tuple[str, str]] | None = None,
    ) -> list[PipelineArtifact]:
        """Artifacts of one session, timeline order (whole-session rows first).

        ``from_ms``/``to_ms`` restrict to span artifacts overlapping the range;
        ``pairs`` to the listed ``(pipeline, kind)`` combinations.
        """
        sql = f"SELECT {COLUMNS} FROM pipeline_artifacts WHERE session_id = %s"
        params: list = [session_id]
        if pipeline is not None:
            sql += " AND pipeline = %s"
            params.append(pipeline)
        if kind is not None:
            sql += " AND kind = %s"
            params.append(kind)
        if pairs is not None:
            if not pairs:
                return []
            sql += " AND (pipeline, kind) IN (" + ", ".join(["(%s, %s)"] * len(pairs)) + ")"
            params += [value for pair in pairs for value in pair]
        if from_ms is not None:
            sql += " AND end_ms > %s"
            params.append(from_ms)
        if to_ms is not None:
            sql += " AND start_ms < %s"
            params.append(to_ms)
        sql += " ORDER BY start_ms NULLS FIRST, id LIMIT %s OFFSET %s"
        params += [limit, offset]
        return await self._fetch_all(PipelineArtifact, sql, params)

    async def delete(self, artifact_id: UUID) -> bool:
        """Delete the row only; any blob it points at is the caller's job."""
        return await self._execute(
            "DELETE FROM pipeline_artifacts WHERE id = %s", (artifact_id,)
        ) > 0
