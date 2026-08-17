"""Raw-SQL repository for the ``recording_sessions`` table."""

from datetime import datetime
from uuid import UUID

from psycopg import errors
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from database.exceptions import NotFoundError
from database.repos.base import BaseRepo
from database.schema.sessions import RecordingSession, SessionCreate

COLUMNS = (
    "id, user_id, device, label, started_at, ended_at, metadata, created_at, updated_at"
)


class SessionsRepo(BaseRepo):
    async def create(self, data: SessionCreate) -> RecordingSession:
        sql = f"""
            INSERT INTO recording_sessions (user_id, device, label, started_at, metadata)
            VALUES (%s, %s, %s, COALESCE(%s, now()), %s)
            RETURNING {COLUMNS}
        """
        params = (
            data.user_id,
            data.device,
            data.label,
            data.started_at,
            Jsonb(data.metadata),
        )
        try:
            async with self._conn.transaction():
                session = await self._fetch_one(RecordingSession, sql, params)
        except errors.ForeignKeyViolation as exc:
            raise NotFoundError("user", data.user_id) from exc
        assert session is not None  # INSERT ... RETURNING always yields a row
        return session

    async def get(self, session_id: UUID) -> RecordingSession | None:
        return await self._fetch_one(
            RecordingSession,
            f"SELECT {COLUMNS} FROM recording_sessions WHERE id = %s",
            (session_id,),
        )

    async def list_for_user(
        self,
        user_id: UUID,
        open_only: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[RecordingSession]:
        sql = f"SELECT {COLUMNS} FROM recording_sessions WHERE user_id = %s"
        if open_only:
            sql += " AND ended_at IS NULL"
        sql += " ORDER BY started_at DESC LIMIT %s OFFSET %s"
        return await self._fetch_all(RecordingSession, sql, (user_id, limit, offset))

    async def overlapping(
        self,
        user_id: UUID,
        start: datetime,
        end: datetime,
        limit: int = 50,
        offset: int = 0,
    ) -> list[RecordingSession]:
        """Sessions touching the wall-clock window ``[start, end)``, oldest
        first; an open session counts as extending to now. Served by
        ``recording_sessions_user_ended_idx`` (0015)."""
        return await self._fetch_all(
            RecordingSession,
            f"""
                SELECT {COLUMNS} FROM recording_sessions
                WHERE user_id = %s AND started_at < %s
                  AND (ended_at IS NULL OR ended_at > %s)
                ORDER BY started_at
                LIMIT %s OFFSET %s
            """,
            (user_id, end, start, limit, offset),
        )

    async def touch(self, session_id: UUID) -> bool:
        """Record activity on an open session, keeping the sweeper away.

        False when the session is missing or has ended; ingest paths treat that
        as "stop writing". The ``updated_at`` bump itself comes from the table's
        BEFORE UPDATE trigger.
        """
        return await self._execute(
            "UPDATE recording_sessions SET updated_at = now() WHERE id = %s AND ended_at IS NULL",
            (session_id,),
        ) > 0

    async def touch_if_stale(
        self, session_id: UUID, min_interval_seconds: float = 60.0
    ) -> bool:
        """Like :meth:`touch`, but writing at most once per interval.

        The per-chunk heartbeat of the streaming path: an unconditional touch
        is a row lock + WAL write per chunk on one hot row, yet the sweeper
        only needs freshness within its stale window. Here the common case is
        one plain read; the UPDATE fires only once ``updated_at`` has actually
        aged. Returns whether the session is open — the read decides that, so
        a session ended between this check and the caller's next write can
        still take one last chunk (the same width of race the sweeper's cutoff
        already tolerates).
        """
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                    SELECT ended_at IS NULL AS open,
                           updated_at < now() - make_interval(secs => %s) AS stale
                    FROM recording_sessions WHERE id = %s
                """,
                (min_interval_seconds, session_id),
            )
            row = await cur.fetchone()
        if row is None or not row["open"]:
            return False
        if row["stale"]:
            await self.touch(session_id)
        return True

    async def end_stale(self, older_than_seconds: float) -> int:
        """End every open session with no activity since the cutoff.

        Idempotent; concurrent sweepers in other workers race harmlessly.
        Returns the number of sessions ended.
        """
        return await self._execute(
            """
                UPDATE recording_sessions SET ended_at = now()
                WHERE ended_at IS NULL
                  AND updated_at < now() - make_interval(secs => %s)
            """,
            (older_than_seconds,),
        )

    async def end(self, session_id: UUID, ended_at: datetime | None = None) -> RecordingSession:
        """Close a session. Re-ending one moves the timestamp."""
        session = await self._fetch_one(
            RecordingSession,
            f"""
                UPDATE recording_sessions SET ended_at = COALESCE(%s, now())
                WHERE id = %s
                RETURNING {COLUMNS}
            """,
            (ended_at, session_id),
        )
        if session is None:
            raise NotFoundError("recording session", session_id)
        return session

    async def split(self, session_id: UUID) -> RecordingSession:
        """End a session as a planned rotation.

        The ``rotated`` metadata marker is what tells a still-connected
        streaming client to reopen (a plain :meth:`end` must not make a
        device immediately re-record). Splitting an already-ended session
        returns it unchanged.
        """
        session = await self._fetch_one(
            RecordingSession,
            f"""
                UPDATE recording_sessions
                SET ended_at = now(), metadata = metadata || '{{"rotated": true}}'::jsonb
                WHERE id = %s AND ended_at IS NULL
                RETURNING {COLUMNS}
            """,
            (session_id,),
        )
        if session is not None:
            return session
        session = await self.get(session_id)
        if session is None:
            raise NotFoundError("recording session", session_id)
        return session

    async def split_all_open(self, user_id: UUID) -> int:
        """Split every open session of one user; returns how many ended."""
        return await self._execute(
            """
                UPDATE recording_sessions
                SET ended_at = now(), metadata = metadata || '{"rotated": true}'::jsonb
                WHERE user_id = %s AND ended_at IS NULL
            """,
            (user_id,),
        )

    async def split_started_before(self, cutoff: datetime) -> int:
        """Split every open session started before the cutoff, any user.

        The scheduled-rotation primitive: bounding by ``started_at`` makes
        re-runs no-ops, so concurrent sweepers and restarts are safe without
        coordination.
        """
        return await self._execute(
            """
                UPDATE recording_sessions
                SET ended_at = now(), metadata = metadata || '{"rotated": true}'::jsonb
                WHERE ended_at IS NULL AND started_at < %s
            """,
            (cutoff,),
        )

    async def set_label(self, session_id: UUID, label: str | None) -> RecordingSession:
        """Rename a session, or clear the name with ``None``.

        Open and ended sessions alike: the label is user curation, not part of
        the capture record.
        """
        session = await self._fetch_one(
            RecordingSession,
            f"""
                UPDATE recording_sessions SET label = %s
                WHERE id = %s
                RETURNING {COLUMNS}
            """,
            (label, session_id),
        )
        if session is None:
            raise NotFoundError("recording session", session_id)
        return session

    async def delete(self, session_id: UUID) -> bool:
        """Delete a session and, by cascade, its segment rows.

        Blobs are not touched; use ``services.segments.delete_session`` for those.
        """
        return await self._execute(
            "DELETE FROM recording_sessions WHERE id = %s", (session_id,)
        ) > 0
