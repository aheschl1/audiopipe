"""Raw-SQL repository for the ``resource_segments`` table.

Blob-backed rows point at objects; the object write is coordinated in
:mod:`services.segments`. Inline rows carry their payload here and never
touch the blob store.
"""

from uuid import UUID

from psycopg import errors
from psycopg.types.json import Jsonb

from database.exceptions import DuplicateSequenceError, NotFoundError
from database.repos.base import BaseRepo
from resources import Resource
from database.schema.segments import (
    ResourceSegment,
    ResourceUsage,
    SegmentCreate,
    SegmentSetFingerprint,
    SessionResourceSummary,
)

COLUMNS = (
    "id, session_id, user_id, resource, sequence, ingested_at, captured_at, offset_ms, "
    "duration_ms, bucket, object_key, payload, byte_size, content_type, checksum_sha256, "
    "attrs, metadata"
)


class SegmentsRepo(BaseRepo):
    async def next_sequence(self, session_id: UUID) -> int:
        """The next sequence number in a session, as of this snapshot.

        One shared space across all resources — the streaming protocol's
        cumulative acks and resume point depend on it. A plain read,
        deliberately not a reservation: locking the session row here would
        hold it for the rest of the caller's transaction — which for an
        ingest spans a blob upload. Two concurrent ingests may read the same
        number; the (session_id, sequence) unique constraint arbitrates, and
        the loser retries with a fresh read
        (:func:`services.segments.ingest_segment`).
        """
        exists = await self._fetch_value(
            "SELECT 1 FROM recording_sessions WHERE id = %s", (session_id,)
        )
        if exists is None:
            raise NotFoundError("recording session", session_id)
        return await self._fetch_value(
            "SELECT COALESCE(MAX(sequence) + 1, 0) FROM resource_segments WHERE session_id = %s",
            (session_id,),
        )

    async def create(self, data: SegmentCreate) -> ResourceSegment:
        """Register a segment (blob already written, or payload inline).

        With ``data.sequence`` unset, the next number in the session is reserved
        via :meth:`next_sequence`.
        """
        sequence = data.sequence
        if sequence is None:
            sequence = await self.next_sequence(data.session_id)

        sql = f"""
            INSERT INTO resource_segments (
                id, session_id, user_id, resource, sequence, captured_at, offset_ms,
                duration_ms, bucket, object_key, payload, byte_size, content_type,
                checksum_sha256, attrs, metadata
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING {COLUMNS}
        """
        params = (
            data.id,
            data.session_id,
            data.user_id,
            data.resource,
            sequence,
            data.captured_at,
            data.offset_ms,
            data.duration_ms,
            data.bucket,
            data.object_key,
            Jsonb(data.payload) if data.payload is not None else None,
            data.byte_size,
            data.content_type,
            data.checksum_sha256,
            Jsonb(data.attrs),
            Jsonb(data.metadata),
        )
        try:
            async with self._conn.transaction():
                segment = await self._fetch_one(ResourceSegment, sql, params)
        except errors.ForeignKeyViolation as exc:
            # The composite FK covers both "no such session" and "session owned
            # by another user".
            raise NotFoundError(
                "recording session for user", (data.session_id, data.user_id)
            ) from exc
        except errors.UniqueViolation as exc:
            # object_key embeds a fresh UUID, so of the two unique constraints
            # only (session_id, sequence) can realistically fire: a retransmit.
            if exc.diag.constraint_name == "resource_segments_session_id_sequence_key":
                raise DuplicateSequenceError(data.session_id, sequence) from exc
            raise
        assert segment is not None  # INSERT ... RETURNING always yields a row
        return segment

    async def get(self, segment_id: UUID) -> ResourceSegment | None:
        return await self._fetch_one(
            ResourceSegment,
            f"SELECT {COLUMNS} FROM resource_segments WHERE id = %s",
            (segment_id,),
        )

    async def list_for_session(
        self,
        session_id: UUID,
        resource: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ResourceSegment]:
        resource_filter = "AND resource = %s" if resource is not None else ""
        params = (session_id, *(() if resource is None else (resource,)), limit, offset)
        return await self._fetch_all(
            ResourceSegment,
            f"""
                SELECT {COLUMNS} FROM resource_segments
                WHERE session_id = %s {resource_filter}
                ORDER BY sequence LIMIT %s OFFSET %s
            """,
            params,
        )

    async def list_for_user(
        self,
        user_id: UUID,
        resource: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ResourceSegment]:
        resource_filter = "AND resource = %s" if resource is not None else ""
        params = (user_id, *(() if resource is None else (resource,)), limit, offset)
        return await self._fetch_all(
            ResourceSegment,
            f"""
                SELECT {COLUMNS} FROM resource_segments
                WHERE user_id = %s {resource_filter}
                ORDER BY ingested_at DESC LIMIT %s OFFSET %s
            """,
            params,
        )

    async def delete(self, segment_id: UUID) -> bool:
        """Delete the row only. Any blob is removed by ``services.segments``."""
        return (
            await self._execute("DELETE FROM resource_segments WHERE id = %s", (segment_id,)) > 0
        )

    async def stitch_fingerprint(
        self, session_id: UUID, resource: Resource = Resource.AUDIO
    ) -> SegmentSetFingerprint:
        """A cheap change-token for a session's segment set of one resource.

        One aggregate row rather than every segment: enough to decide whether a
        cached stitch plan is still valid without loading the rows behind it.
        Scoped per resource — an interleaved row of another resource must not
        move the audio fingerprint.
        """
        fingerprint = await self._fetch_one(
            SegmentSetFingerprint,
            """
                SELECT COUNT(*) AS count,
                       COALESCE(MAX(sequence), -1) AS max_sequence,
                       COALESCE(SUM(byte_size), 0) AS total_bytes
                FROM resource_segments WHERE session_id = %s AND resource = %s
            """,
            (session_id, resource),
        )
        assert fingerprint is not None  # aggregate without GROUP BY always returns a row
        return fingerprint

    async def usage_for_user(self, user_id: UUID) -> list[ResourceUsage]:
        """Stored totals per resource; a resource never captured has no row."""
        return await self._fetch_all(
            ResourceUsage,
            """
                SELECT resource, COUNT(*) AS segments,
                       COALESCE(SUM(byte_size), 0) AS total_bytes
                FROM resource_segments WHERE user_id = %s
                GROUP BY resource ORDER BY resource
            """,
            (user_id,),
        )

    async def resource_summary(self, session_id: UUID) -> list[SessionResourceSummary]:
        """What a session holds, one row per resource present."""
        return await self._fetch_all(
            SessionResourceSummary,
            """
                SELECT resource, COUNT(*) AS segments,
                       COALESCE(SUM(byte_size), 0) AS total_bytes,
                       MIN(captured_at) AS first_captured_at,
                       MAX(captured_at) AS last_captured_at
                FROM resource_segments WHERE session_id = %s
                GROUP BY resource ORDER BY resource
            """,
            (session_id,),
        )
