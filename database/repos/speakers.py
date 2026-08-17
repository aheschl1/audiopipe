"""Raw-SQL repository for ``speakers`` and the clustering statements.

Every operation that computes a distance or an average filters by
``(user_id, model)``: pgvector's ``<=>`` and ``avg()`` error out on mixed
dimensions, and embeddings from different models are not comparable anyway.
Vector literals cross the wire in text form with ``::vector`` casts, same as
:mod:`database.repos.embeddings`.
"""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from psycopg.rows import dict_row

from database.repos.base import BaseRepo
from database.schema.speakers import (
    CorpusEmbedding,
    EmbeddingModelCount,
    ProjectionPoint,
    SessionSpeakerLabel,
    Speaker,
    SpeakerCreate,
    SpeakerMember,
    SpeakerSummary,
    SpeakerTalkTime,
    SpeakerTranscript,
)

COLUMNS = "id, user_id, name, model, centroid::text AS centroid, created_at, updated_at"

_TRANSCRIPT_COLUMNS = """
    t.id AS artifact_id, t.session_id, e.speaker_id, e.speaker,
    t.start_ms, t.end_ms,
    coalesce(t.metadata->>'text', '') AS text, t.metadata->>'model' AS model,
    rs.started_at + make_interval(secs => t.start_ms / 1000.0) AS occurred_at
"""


def _literal(vector: Sequence[float]) -> str:
    return "[" + ",".join(str(value) for value in vector) + "]"


class SpeakersRepo(BaseRepo):
    async def create(self, data: SpeakerCreate) -> Speaker:
        speaker = await self._fetch_one(
            Speaker,
            f"""
                INSERT INTO speakers (user_id, name, model, centroid)
                VALUES (%s, %s, %s, %s::vector)
                RETURNING {COLUMNS}
            """,
            (data.user_id, data.name, data.model, _literal(data.centroid)),
        )
        assert speaker is not None  # INSERT ... RETURNING always yields a row
        return speaker

    async def get(self, speaker_id: UUID) -> Speaker | None:
        return await self._fetch_one(
            Speaker,
            f"SELECT {COLUMNS} FROM speakers WHERE id = %s",
            (speaker_id,),
        )

    async def list_for_user(
        self,
        user_id: UUID,
        name: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SpeakerSummary]:
        """The user's clusters with membership counts, named or not.

        LEFT JOIN: a named cluster whose members were cascaded away by a
        diarize republish must stay visible — it anchors the identity that
        reclaims its members on re-cluster.
        """
        sql = f"""
            SELECT s.id, s.user_id, s.name, s.model,
                   s.centroid::text AS centroid, s.created_at, s.updated_at,
                   count(e.artifact_id) AS embeddings,
                   count(DISTINCT e.session_id) AS sessions
            FROM speakers s
            LEFT JOIN speaker_embeddings e ON e.speaker_id = s.id
            WHERE s.user_id = %s
        """
        params: list = [user_id]
        if name is not None:
            sql += " AND s.name = %s"
            params.append(name)
        sql += " GROUP BY s.id ORDER BY s.created_at, s.id LIMIT %s OFFSET %s"
        params += [limit, offset]
        return await self._fetch_all(SpeakerSummary, sql, params)

    async def summarize(self, speaker_id: UUID) -> SpeakerSummary | None:
        """One cluster with its membership counts."""
        return await self._fetch_one(
            SpeakerSummary,
            """
                SELECT s.id, s.user_id, s.name, s.model,
                       s.centroid::text AS centroid, s.created_at, s.updated_at,
                       count(e.artifact_id) AS embeddings,
                       count(DISTINCT e.session_id) AS sessions
                FROM speakers s
                LEFT JOIN speaker_embeddings e ON e.speaker_id = s.id
                WHERE s.id = %s
                GROUP BY s.id
            """,
            (speaker_id,),
        )

    async def set_name(self, speaker_id: UUID, name: str | None) -> Speaker | None:
        return await self._fetch_one(
            Speaker,
            f"UPDATE speakers SET name = %s WHERE id = %s RETURNING {COLUMNS}",
            (name, speaker_id),
        )


    async def corpus_for_user(self, user_id: UUID) -> list[CorpusEmbedding]:
        """Every voice-print of the user with its previous assignment and pin
        — the batch clusterer's whole input, in deterministic order.

        ``talk_ms`` prefers ``clean_talk_ms`` (non-overlapped speech) when
        the diarizer recorded it: a print whose label only ever spoke under
        crosstalk is exactly the unreliable kind the talk gate exists to
        skip. Prints from before overlap tracking fall back to raw talk."""
        return await self._fetch_all(
            CorpusEmbedding,
            """
                SELECT e.artifact_id, e.session_id, e.speaker, e.model,
                       e.embedding::text AS embedding,
                       coalesce((a.metadata->>'clean_talk_ms')::bigint,
                                (a.metadata->>'talk_ms')::bigint) AS talk_ms,
                       e.speaker_id, p.speaker_id AS pinned_to
                FROM speaker_embeddings e
                JOIN recording_sessions rs ON rs.id = e.session_id
                LEFT JOIN pipeline_artifacts a ON a.id = e.artifact_id
                LEFT JOIN speaker_pins p ON p.session_id = e.session_id
                    AND p.speaker = e.speaker AND p.model = e.model
                WHERE rs.user_id = %s
                ORDER BY e.artifact_id
            """,
            (user_id,),
        )

    async def projection_corpus(
        self, user_id: UUID, model: str
    ) -> list[ProjectionPoint]:
        """One model's voice-prints with their inspection metadata.

        A sibling of :meth:`corpus_for_user` rather than an extension of it —
        the clusterer wants every model in one pass, the map wants exactly one
        (a shared basis across models would be meaningless) plus the cluster
        name, centroid distance and clip span it does not need.

        The clip is the voice's cleanest utterance in the block, by the same
        join :meth:`members` uses, so both views play the same audio."""
        return await self._fetch_all(
            ProjectionPoint,
            """
                SELECT e.artifact_id, e.session_id, e.speaker,
                       e.embedding::text AS embedding,
                       rs.started_at, rs.label AS session_label,
                       a.start_ms, a.end_ms,
                       coalesce((a.metadata->>'clean_talk_ms')::bigint,
                                (a.metadata->>'talk_ms')::bigint) AS talk_ms,
                       a.metadata->>'split_of' AS split_of,
                       e.speaker_id, s.name AS speaker_name,
                       CASE WHEN s.centroid IS NULL THEN NULL
                            ELSE e.embedding <=> s.centroid END AS distance,
                       p.speaker_id IS NOT NULL AS pinned,
                       u.start_ms AS clip_start_ms, u.end_ms AS clip_end_ms
                FROM speaker_embeddings e
                JOIN recording_sessions rs ON rs.id = e.session_id
                LEFT JOIN speakers s ON s.id = e.speaker_id
                LEFT JOIN pipeline_artifacts a ON a.id = e.artifact_id
                LEFT JOIN speaker_pins p ON p.session_id = e.session_id
                    AND p.speaker = e.speaker AND p.model = e.model
                LEFT JOIN LATERAL (
                    SELECT t.start_ms, t.end_ms FROM pipeline_artifacts t
                    WHERE t.session_id = e.session_id
                      AND t.pipeline = 'diarize' AND t.kind = 'utterance'
                      AND t.metadata->>'speaker' = e.speaker
                    ORDER BY (t.end_ms - t.start_ms)
                             - coalesce((t.metadata->>'overlap_ms')::bigint, 0) DESC
                    LIMIT 1
                ) u ON true
                WHERE rs.user_id = %s AND e.model = %s
                ORDER BY e.artifact_id
            """,
            (user_id, model),
        )

    async def embedding_models(self, user_id: UUID) -> list[EmbeddingModelCount]:
        """The models present in a user's corpus, largest first — the map's
        picker and its default."""
        return await self._fetch_all(
            EmbeddingModelCount,
            """
                SELECT e.model, count(*) AS embeddings
                FROM speaker_embeddings e
                JOIN recording_sessions rs ON rs.id = e.session_id
                WHERE rs.user_id = %s
                GROUP BY e.model
                ORDER BY count(*) DESC, e.model
            """,
            (user_id,),
        )

    async def similar(
        self,
        user_id: UUID,
        model: str,
        vector: Sequence[float],
        exclude_id: UUID,
        limit: int = 20,
    ) -> list[tuple[SpeakerSummary, float]]:
        """The user's other clusters in this model, closest centroid first —
        the candidate list for a manual merge, distances included so the UI
        can show how far apart two voices really are."""
        literal = _literal(vector)
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                    SELECT s.id, s.user_id, s.name, s.model,
                           s.centroid::text AS centroid, s.created_at, s.updated_at,
                           count(e.artifact_id) AS embeddings,
                           count(DISTINCT e.session_id) AS sessions,
                           s.centroid <=> %s::vector AS distance
                    FROM speakers s
                    LEFT JOIN speaker_embeddings e ON e.speaker_id = s.id
                    WHERE s.user_id = %s AND s.model = %s AND s.id != %s
                    GROUP BY s.id
                    ORDER BY distance, s.id
                    LIMIT %s
                """,
                (literal, user_id, model, exclude_id, limit),
            )
            rows = await cur.fetchall()
        ranked: list[tuple[SpeakerSummary, float]] = []
        for row in rows:
            distance = float(row.pop("distance"))
            ranked.append((SpeakerSummary.model_validate(row), distance))
        return ranked

    async def clear_assignments(self, user_id: UUID, model: str) -> int:
        """Rebuild prep: detach every embedding of one (user, model) corpus.
        Assignments are derived data, rewritten wholesale each batch run."""
        return await self._execute(
            """
                UPDATE speaker_embeddings e SET speaker_id = NULL
                FROM recording_sessions rs
                WHERE rs.id = e.session_id AND rs.user_id = %s
                  AND e.model = %s AND e.speaker_id IS NOT NULL
            """,
            (user_id, model),
        )

    async def set_assignments(
        self, speaker_id: UUID, artifact_ids: Sequence[UUID]
    ) -> int:
        return await self._execute(
            "UPDATE speaker_embeddings SET speaker_id = %s WHERE artifact_id = ANY(%s)",
            (speaker_id, list(artifact_ids)),
        )


    async def pin(self, artifact_id: UUID, speaker_id: UUID) -> None:
        """Assert a voice-print's identity; every future rebuild honors it.

        Keyed on (session, label, model) — the address a re-diarize
        reproduces — so pins outlive artifact republication."""
        await self._execute(
            """
                INSERT INTO speaker_pins (session_id, speaker, model, speaker_id)
                SELECT e.session_id, e.speaker, e.model, %s
                FROM speaker_embeddings e WHERE e.artifact_id = %s
                ON CONFLICT (session_id, speaker, model) DO UPDATE
                    SET speaker_id = EXCLUDED.speaker_id
            """,
            (speaker_id, artifact_id),
        )

    async def reassign(
        self, artifact_id: UUID, from_speaker_id: UUID, to_speaker_id: UUID
    ) -> bool:
        """Move one voice-print between clusters. Rowcount-guarded on the
        source: 0 means membership changed under the caller (a rebuild ran
        between their read and this write) — surface, don't guess."""
        moved = await self._execute(
            """
                UPDATE speaker_embeddings SET speaker_id = %s
                WHERE artifact_id = %s AND speaker_id = %s
            """,
            (to_speaker_id, artifact_id, from_speaker_id),
        ) > 0
        if moved:
            await self.recompute_centroid(from_speaker_id)
            await self.recompute_centroid(to_speaker_id)
        return moved

    async def unpin_member(self, artifact_id: UUID, speaker_id: UUID) -> bool:
        """Drop a pin, guarded by current membership of the given cluster."""
        return await self._execute(
            """
                DELETE FROM speaker_pins p
                USING speaker_embeddings e
                WHERE e.artifact_id = %s AND p.session_id = e.session_id
                  AND p.speaker = e.speaker AND p.model = e.model
                  AND e.speaker_id = %s
            """,
            (artifact_id, speaker_id),
        ) > 0

    async def pin_members_of(self, speaker_id: UUID, target_id: UUID) -> int:
        """Pin every current member of one cluster to an identity — how a
        manual merge survives batch rebuilds."""
        return await self._execute(
            """
                INSERT INTO speaker_pins (session_id, speaker, model, speaker_id)
                SELECT session_id, speaker, model, %s FROM speaker_embeddings
                WHERE speaker_id = %s
                ON CONFLICT (session_id, speaker, model) DO UPDATE
                    SET speaker_id = EXCLUDED.speaker_id
            """,
            (target_id, speaker_id),
        )

    async def recompute_centroid(self, speaker_id: UUID) -> None:
        """Centroid = mean of current members; an empty cluster keeps its
        last centroid as the identity anchor (avg over zero rows is NULL)."""
        await self._execute(
            """
                UPDATE speakers s SET centroid = m.c
                FROM (
                    SELECT avg(embedding) AS c FROM speaker_embeddings
                    WHERE speaker_id = %s
                ) m
                WHERE s.id = %s AND m.c IS NOT NULL
            """,
            (speaker_id, speaker_id),
        )

    async def members(
        self, speaker_id: UUID, limit: int = 50, offset: int = 0
    ) -> list[SpeakerMember]:
        """A cluster's voice-prints, farthest from the centroid first — the
        inspection order: outliers (likely wrong voices) surface on top. The
        clip is the voice's *cleanest* utterance in that block — most
        non-overlapped audio, i.e. duration minus crosstalk — via the same
        (session, namespaced-label) join the transcript routes use. Longest
        would systematically pick the worst clip in a crowded scene: other
        people talking over are in the rendered audio, and a listener would
        blame the voice-print for voices that were never assigned to it."""
        return await self._fetch_all(
            SpeakerMember,
            """
                SELECT e.artifact_id, e.session_id, rs.started_at, e.speaker,
                       coalesce((a.metadata->>'clean_talk_ms')::bigint,
                                (a.metadata->>'talk_ms')::bigint) AS talk_ms,
                       e.embedding <=> s.centroid AS distance,
                       p.speaker_id IS NOT NULL AS pinned,
                       u.start_ms AS clip_start_ms, u.end_ms AS clip_end_ms
                FROM speaker_embeddings e
                JOIN speakers s ON s.id = e.speaker_id
                JOIN recording_sessions rs ON rs.id = e.session_id
                LEFT JOIN pipeline_artifacts a ON a.id = e.artifact_id
                LEFT JOIN speaker_pins p ON p.session_id = e.session_id
                    AND p.speaker = e.speaker AND p.model = e.model
                LEFT JOIN LATERAL (
                    SELECT t.start_ms, t.end_ms FROM pipeline_artifacts t
                    WHERE t.session_id = e.session_id
                      AND t.pipeline = 'diarize' AND t.kind = 'utterance'
                      AND t.metadata->>'speaker' = e.speaker
                    ORDER BY (t.end_ms - t.start_ms)
                             - coalesce((t.metadata->>'overlap_ms')::bigint, 0) DESC
                    LIMIT 1
                ) u ON true
                WHERE e.speaker_id = %s
                ORDER BY distance DESC, e.artifact_id
                LIMIT %s OFFSET %s
            """,
            (speaker_id, limit, offset),
        )

    async def list_for_user_model(self, user_id: UUID, model: str) -> list[Speaker]:
        return await self._fetch_all(
            Speaker,
            f"""
                SELECT {COLUMNS} FROM speakers
                WHERE user_id = %s AND model = %s
                ORDER BY created_at, id
            """,
            (user_id, model),
        )

    async def merge(self, loser_id: UUID, survivor_id: UUID) -> None:
        """Fold one cluster into another; the survivor keeps id and name."""
        await self._execute(
            "UPDATE speaker_embeddings SET speaker_id = %s WHERE speaker_id = %s",
            (survivor_id, loser_id),
        )
        await self._execute("DELETE FROM speakers WHERE id = %s", (loser_id,))
        await self.recompute_centroid(survivor_id)

    async def delete_empty_unlabeled(self, user_id: UUID, model: str) -> int:
        """Drop unnamed clusters with no members (republication leftovers);
        named empties survive as identity anchors."""
        return await self._execute(
            """
                DELETE FROM speakers s
                WHERE s.user_id = %s AND s.model = %s AND s.name IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM speaker_embeddings e WHERE e.speaker_id = s.id
                  )
            """,
            (user_id, model),
        )

    # ------------------------------------------------------------- read side

    async def talk_time(
        self,
        user_id: UUID,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[SpeakerTalkTime]:
        """Total talk time per clustered speaker, most-talkative first,
        bounded at session granularity by ``since``/``until``."""
        conditions = ["rs.user_id = %s"]
        params: list = [user_id]
        if since is not None:
            conditions.append("(rs.ended_at IS NULL OR rs.ended_at > %s)")
            params.append(since)
        if until is not None:
            conditions.append("rs.started_at < %s")
            params.append(until)
        return await self._fetch_all(
            SpeakerTalkTime,
            f"""
                SELECT sp.id AS speaker_id, sp.name,
                       sum(coalesce((a.metadata->>'clean_talk_ms')::bigint,
                                    (a.metadata->>'talk_ms')::bigint)) AS talk_ms
                FROM speaker_embeddings e
                JOIN recording_sessions rs ON rs.id = e.session_id
                JOIN speakers sp ON sp.id = e.speaker_id
                LEFT JOIN pipeline_artifacts a ON a.id = e.artifact_id
                WHERE {" AND ".join(conditions)}
                GROUP BY sp.id, sp.name
                ORDER BY talk_ms DESC NULLS LAST, sp.id
            """,
            params,
        )

    async def labels_for_session(self, session_id: UUID) -> list[SessionSpeakerLabel]:
        """Every block-local label of a session with its cluster resolution
        (unassigned labels included, speaker_id NULL — nothing is hidden)."""
        return await self._fetch_all(
            SessionSpeakerLabel,
            """
                SELECT e.speaker, e.artifact_id, e.speaker_id, s.name,
                       coalesce((a.metadata->>'clean_talk_ms')::bigint,
                                (a.metadata->>'talk_ms')::bigint) AS talk_ms
                FROM speaker_embeddings e
                LEFT JOIN speakers s ON s.id = e.speaker_id
                LEFT JOIN pipeline_artifacts a ON a.id = e.artifact_id
                WHERE e.session_id = %s
                ORDER BY e.speaker
            """,
            (session_id,),
        )

    async def transcripts_for_speaker(
        self, speaker_id: UUID, limit: int = 50, offset: int = 0
    ) -> list[SpeakerTranscript]:
        return await self._transcripts("s.id = %s", [speaker_id], limit, offset)

    async def transcripts_for_name(
        self, user_id: UUID, name: str, limit: int = 50, offset: int = 0
    ) -> list[SpeakerTranscript]:
        """Everything said by every cluster sharing this name — names are
        non-unique by design, so this is the complete-retrieval path."""
        return await self._transcripts(
            "s.user_id = %s AND s.name = %s", [user_id, name], limit, offset
        )

    async def _transcripts(
        self, where: str, params: list, limit: int, offset: int
    ) -> list[SpeakerTranscript]:
        # One embedding per (session, label) — UNIQUE in the schema — so the
        # join cannot duplicate transcript rows. The predicate matches
        # pipeline_artifacts_transcript_speaker_idx exactly.
        return await self._fetch_all(
            SpeakerTranscript,
            f"""
                SELECT {_TRANSCRIPT_COLUMNS}
                FROM speakers s
                JOIN speaker_embeddings e ON e.speaker_id = s.id
                JOIN recording_sessions rs ON rs.id = e.session_id
                JOIN pipeline_artifacts t ON t.session_id = e.session_id
                    AND t.pipeline = 'transcribe' AND t.kind = 'transcript'
                    AND t.metadata->>'speaker' = e.speaker
                WHERE {where}
                ORDER BY rs.started_at DESC, t.start_ms, t.id
                LIMIT %s OFFSET %s
            """,
            params + [limit, offset],
        )
