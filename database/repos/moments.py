"""Moments and the artifact links they assert."""

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict

from database.repos.base import BaseRepo

COLUMNS = "id, user_id, start_at, end_at, title, summary, session_ids, model, metadata, created_at"
_M_COLUMNS = ", ".join(f"m.{c.strip()}" for c in COLUMNS.split(","))


class Moment(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    user_id: UUID
    start_at: datetime
    end_at: datetime
    title: str
    summary: str
    session_ids: list[UUID]
    model: str
    metadata: dict[str, Any]
    created_at: datetime


class MomentCreate(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: UUID
    start_at: datetime
    end_at: datetime
    title: str
    summary: str
    session_ids: list[UUID]
    model: str
    metadata: dict[str, Any]


class LinkCreate(BaseModel):
    model_config = ConfigDict(frozen=True)

    from_artifact: UUID
    to_artifact: UUID
    relation: str
    confidence: float
    why: str


class ArtifactLink(LinkCreate):
    moment_id: UUID


class MomentsRepo(BaseRepo):
    async def replace(
        self,
        user_id: UUID,
        start_at: datetime,
        end_at: datetime,
        moments: Sequence[tuple[MomentCreate, Sequence[LinkCreate]]],
    ) -> list[Moment]:
        """Drop the user's moments overlapping [start_at, end_at], then insert these.

        Call inside one transaction (a DatabasePipe block) so readers never see
        the window empty.
        """
        await self._execute(
            "DELETE FROM moments WHERE user_id = %s AND start_at <= %s AND end_at >= %s",
            (user_id, end_at, start_at),
        )
        created = []
        for moment, links in moments:
            row = await self._fetch_one(
                Moment,
                f"""
                    INSERT INTO moments
                        (user_id, start_at, end_at, title, summary, session_ids, model, metadata)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING {COLUMNS}
                """,
                (
                    moment.user_id, moment.start_at, moment.end_at, moment.title,
                    moment.summary, moment.session_ids, moment.model,
                    Jsonb(moment.metadata),
                ),
            )
            assert row is not None
            if links:
                params: list = []
                for link in links:
                    params += [row.id, link.from_artifact, link.to_artifact, link.relation,
                               link.confidence, link.why]
                # Only links whose artifacts still exist: one may have been
                # republished away since the moment's items were read.
                await self._execute(
                    f"""
                        INSERT INTO artifact_links
                            (moment_id, from_artifact, to_artifact, relation, confidence, why)
                        SELECT v.* FROM (VALUES {", ".join(["(%s::uuid, %s::uuid, %s::uuid, %s, %s::real, %s)"] * len(links))})
                            AS v(moment_id, from_artifact, to_artifact, relation, confidence, why)
                        WHERE EXISTS (SELECT 1 FROM pipeline_artifacts a WHERE a.id = v.from_artifact)
                          AND EXISTS (SELECT 1 FROM pipeline_artifacts a WHERE a.id = v.to_artifact)
                        ON CONFLICT DO NOTHING
                    """,
                    params,
                )
            created.append(row)
        return created

    async def prune_orphans(self) -> int:
        """Delete moments none of whose sessions still exist."""
        return await self._execute(
            """
                DELETE FROM moments m WHERE NOT EXISTS (
                    SELECT 1 FROM recording_sessions s WHERE s.id = ANY(m.session_ids)
                )
            """
        )

    async def list_for_user(
        self, user_id: UUID, *, before: datetime | None = None, limit: int = 20
    ) -> list[Moment]:
        """Newest first, starting strictly before ``before``."""
        conditions, params = ["user_id = %s"], [user_id]
        if before is not None:
            conditions.append("start_at < %s")
            params.append(before)
        return await self._fetch_all(
            Moment,
            f"""
                SELECT {COLUMNS} FROM moments WHERE {" AND ".join(conditions)}
                ORDER BY start_at DESC, id LIMIT %s
            """,
            [*params, limit],
        )

    async def building(self, user_id: UUID) -> int:
        """Moment jobs queued or running for this user."""
        count = await self._fetch_value(
            """
                SELECT count(*) FROM processing_jobs
                WHERE pipeline = 'moment' AND status IN ('queued', 'running')
                  AND payload->>'user_id' = %s
            """,
            (str(user_id),),
        )
        return int(count or 0)

    async def get(self, moment_id: UUID, user_id: UUID) -> Moment | None:
        return await self._fetch_one(
            Moment,
            f"SELECT {COLUMNS} FROM moments WHERE id = %s AND user_id = %s",
            (moment_id, user_id),
        )

    async def neighbours(self, moment: Moment) -> tuple[UUID | None, UUID | None]:
        """The user's moments just before and just after this one."""
        before = await self._fetch_value(
            """
                SELECT id FROM moments WHERE user_id = %s AND (start_at, id) < (%s, %s)
                ORDER BY start_at DESC, id DESC LIMIT 1
            """,
            (moment.user_id, moment.start_at, moment.id),
        )
        after = await self._fetch_value(
            """
                SELECT id FROM moments WHERE user_id = %s AND (start_at, id) > (%s, %s)
                ORDER BY start_at, id LIMIT 1
            """,
            (moment.user_id, moment.start_at, moment.id),
        )
        return before, after

    async def at(self, user_id: UUID, session_id: UUID, at_ms: int) -> Moment | None:
        """The moment of this session covering an instant, else the nearest within an hour."""
        return await self._fetch_one(
            Moment,
            f"""
                SELECT {_M_COLUMNS} FROM moments m, recording_sessions s,
                    LATERAL (SELECT s.started_at + make_interval(secs => %s / 1000.0) AS t) p
                WHERE s.id = %s AND s.user_id = %s AND m.user_id = %s
                  AND s.id = ANY(m.session_ids)
                  AND p.t BETWEEN m.start_at - interval '1 hour' AND m.end_at + interval '1 hour'
                ORDER BY greatest(m.start_at - p.t, p.t - m.end_at, interval '0'), m.start_at
                LIMIT 1
            """,
            (at_ms, session_id, user_id, user_id),
        )

    async def links(self, moment_id: UUID) -> list[ArtifactLink]:
        return await self._fetch_all(
            ArtifactLink,
            """
                SELECT moment_id, from_artifact, to_artifact, relation, confidence, why
                FROM artifact_links WHERE moment_id = %s
                ORDER BY confidence DESC, from_artifact, to_artifact
            """,
            (moment_id,),
        )

    async def links_for_artifact(self, artifact_id: UUID, user_id: UUID) -> list[ArtifactLink]:
        """Every link touching one artifact, from moments the user owns."""
        return await self._fetch_all(
            ArtifactLink,
            """
                SELECT l.moment_id, l.from_artifact, l.to_artifact, l.relation, l.confidence, l.why
                FROM artifact_links l JOIN moments m ON m.id = l.moment_id
                WHERE (l.from_artifact = %s OR l.to_artifact = %s) AND m.user_id = %s
                ORDER BY l.confidence DESC
            """,
            (artifact_id, artifact_id, user_id),
        )
