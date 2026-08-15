"""The single entry point into the database.

    async with DatabasePipe() as pipe:
        user = await pipe.users.get_by_email("a@b.com")
        issued = await pipe.tokens.issue(user.id, name="cli")

Everything done inside one block runs on one connection inside one
transaction: it commits when the block exits cleanly and rolls back if an
exception escapes.
"""

import asyncio
from functools import cached_property
from types import TracebackType
from typing import Self

from psycopg import AsyncConnection, AsyncTransaction
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from database.config import DatabaseSettings, get_settings
from database.repos.ab_votes import AbVotesRepo
from database.repos.artifacts import ArtifactsRepo
from database.repos.cluster_params import ClusterParamsRepo
from database.repos.embeddings import AudioEmbeddingsRepo, EmbeddingsRepo
from database.repos.jobs import JobsRepo
from database.repos.locations import LocationsRepo
from database.repos.segments import SegmentsRepo
from database.repos.sessions import SessionsRepo
from database.repos.speakers import SpeakersRepo
from database.repos.tags import TagsRepo
from database.repos.transcripts import TranscriptSearchRepo
from database.repos.tokens import TokensRepo
from database.repos.users import UsersRepo

_pool: AsyncConnectionPool | None = None
_pool_lock: asyncio.Lock | None = None
_pool_loop: asyncio.AbstractEventLoop | None = None


def _lock() -> asyncio.Lock:
    """The creation lock for the running loop.

    An asyncio.Lock binds to the loop that first awaits it, so a process that
    runs more than one loop over its lifetime needs a lock per loop. A server
    has exactly one; a test suite has one per test.
    """
    global _pool_lock, _pool_loop
    loop = asyncio.get_running_loop()
    if _pool_lock is None or _pool_loop is not loop:
        _pool_lock = asyncio.Lock()
        _pool_loop = loop
    return _pool_lock


async def get_pool(settings: DatabaseSettings | None = None) -> AsyncConnectionPool:
    """The process-wide connection pool, opened on first use."""
    global _pool
    if _pool is None:
        async with _lock():
            if _pool is None:
                settings = settings or get_settings()
                pool = AsyncConnectionPool(
                    settings.conninfo,
                    min_size=settings.pool_min_size,
                    max_size=settings.pool_max_size,
                    kwargs={"row_factory": dict_row},
                    open=False,
                )
                await pool.open(wait=True, timeout=settings.connect_timeout)
                _pool = pool
    return _pool


async def close_pool() -> None:
    """Close the pool. Call once on application shutdown."""
    global _pool, _pool_lock, _pool_loop
    async with _lock():
        if _pool is not None:
            await _pool.close()
            _pool = None
    _pool_lock = _pool_loop = None


class DatabasePipe:
    """Borrows a connection and exposes the typed repositories on it."""

    def __init__(
        self,
        *,
        pool: AsyncConnectionPool | None = None,
        settings: DatabaseSettings | None = None,
    ) -> None:
        self._explicit_pool = pool
        self._settings = settings
        self._cm = None
        self._tx: AsyncTransaction | None = None
        self._conn: AsyncConnection | None = None

    @property
    def connection(self) -> AsyncConnection:
        """The underlying connection, for one-off SQL outside a repository."""
        if self._conn is None:
            raise RuntimeError("DatabasePipe is only usable inside 'async with'")
        return self._conn

    # One cached_property per repository: built on first access against the
    # borrowed connection, dropped on exit by _clear_repos.

    @cached_property
    def users(self) -> UsersRepo:
        return UsersRepo(self.connection)

    @cached_property
    def tokens(self) -> TokensRepo:
        return TokensRepo(self.connection)

    @cached_property
    def sessions(self) -> SessionsRepo:
        return SessionsRepo(self.connection)

    @cached_property
    def segments(self) -> SegmentsRepo:
        return SegmentsRepo(self.connection)

    @cached_property
    def locations(self) -> LocationsRepo:
        return LocationsRepo(self.connection)

    @cached_property
    def jobs(self) -> JobsRepo:
        return JobsRepo(self.connection)

    @cached_property
    def artifacts(self) -> ArtifactsRepo:
        return ArtifactsRepo(self.connection)

    @cached_property
    def embeddings(self) -> EmbeddingsRepo:
        return EmbeddingsRepo(self.connection)

    @cached_property
    def audio_embeddings(self) -> AudioEmbeddingsRepo:
        return AudioEmbeddingsRepo(self.connection)

    @cached_property
    def speakers(self) -> SpeakersRepo:
        return SpeakersRepo(self.connection)

    @cached_property
    def cluster_params(self) -> ClusterParamsRepo:
        return ClusterParamsRepo(self.connection)

    @cached_property
    def ab_votes(self) -> AbVotesRepo:
        return AbVotesRepo(self.connection)

    @cached_property
    def tags(self) -> TagsRepo:
        return TagsRepo(self.connection)

    @cached_property
    def transcripts(self) -> TranscriptSearchRepo:
        return TranscriptSearchRepo(self.connection)

    def _clear_repos(self) -> None:
        """Drop cached repositories so none outlives its connection."""
        for name, attribute in vars(type(self)).items():
            if isinstance(attribute, cached_property):
                self.__dict__.pop(name, None)

    async def ping(self) -> bool:
        async with self.connection.cursor() as cur:
            await cur.execute("SELECT 1")
            return await cur.fetchone() is not None

    async def __aenter__(self) -> Self:
        pool = self._explicit_pool or await get_pool(self._settings)
        self._cm = pool.connection()
        conn = await self._cm.__aenter__()
        # Opening the transaction here makes it the outermost one, so any
        # conn.transaction() a repository opens is a nested SAVEPOINT and cannot
        # commit the block's work early.
        self._tx = conn.transaction()
        await self._tx.__aenter__()
        self._conn = conn
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        cm, tx = self._cm, self._tx
        self._cm = self._tx = self._conn = None
        self._clear_repos()
        if cm is None:
            return None
        try:
            # Commits on a clean exit, rolls back if an exception is escaping.
            if tx is not None:
                await tx.__aexit__(exc_type, exc, tb)
        finally:
            # Returns the connection to the pool.
            await cm.__aexit__(exc_type, exc, tb)
        return None
