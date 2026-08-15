"""Cache of each session's stitched-audio representation.

Building the representation -- the shared WAV header, the byte-offset plan, and
the strong ETag -- reads every segment row and hashes the whole set. Redoing
that on every conditional check or range seek is the audio route's dominant
cost, so the result is cached and reused until the session's segments actually
change.

A cheap per-session fingerprint (one aggregate query, no rows across the wire)
gates the cache and is read on every request, so a hit never touches the rows
and the cache stays coherent across workers: a write in another process moves
the fingerprint, which every worker sees on its next request.
"""

import hashlib
from collections import OrderedDict
from dataclasses import dataclass
from uuid import UUID

from services import stitch
from database.pipe import DatabasePipe
from resources import Resource
from database.schema.segments import ResourceSegment, SegmentSetFingerprint
from database.schema.sessions import RecordingSession
from storage.pipe import BlobPipe

# Far past any real session (one chunk per second for a day is 86 400).
_MAX_STITCH_SEGMENTS = 1_000_000

# Cap the total pieces held across every cached plan. A plan's memory is
# dominated by its per-segment object keys, so bounding pieces bounds memory;
# entries evict least-recently-used once the budget is exceeded.
_PIECE_BUDGET = 500_000


@dataclass(frozen=True, slots=True)
class SessionAudio:
    """The request-independent, cacheable view of a session's stitched audio."""

    etag: str
    header: bytes
    plan: stitch.StitchPlan


def _session_etag(segments: list[ResourceSegment]) -> str:
    """Strong validator over the exact set of stitched segments."""
    digest = hashlib.sha256()
    for segment in segments:
        digest.update(segment.id.bytes)
        digest.update(segment.checksum_sha256 or segment.byte_size.to_bytes(8, "big"))
    return f'"{digest.hexdigest()}"'


@dataclass(slots=True)
class _Entry:
    fingerprint: SegmentSetFingerprint
    audio: SessionAudio
    pieces: int


class _RepresentationCache:
    """Session -> representation, valid while the fingerprint is unchanged.

    Bounded by a total-pieces budget rather than an entry count, since one huge
    session costs far more than many small ones. Access is single-threaded
    asyncio, so the plain dict operations are atomic and need no lock.
    """

    def __init__(self, piece_budget: int) -> None:
        self._budget = piece_budget
        self._entries: OrderedDict[UUID, _Entry] = OrderedDict()
        self._pieces = 0

    def get(
        self, session_id: UUID, fingerprint: SegmentSetFingerprint
    ) -> SessionAudio | None:
        entry = self._entries.get(session_id)
        if entry is None or entry.fingerprint != fingerprint:
            return None
        self._entries.move_to_end(session_id)
        return entry.audio

    def put(
        self, session_id: UUID, fingerprint: SegmentSetFingerprint, audio: SessionAudio
    ) -> None:
        old = self._entries.pop(session_id, None)
        if old is not None:
            self._pieces -= old.pieces
        pieces = len(audio.plan.pieces)
        # A session larger than the whole budget is served but never cached, so
        # one giant session cannot evict everything else.
        if pieces > self._budget:
            return
        self._entries[session_id] = _Entry(fingerprint, audio, pieces)
        self._pieces += pieces
        while self._pieces > self._budget:
            _, evicted = self._entries.popitem(last=False)
            self._pieces -= evicted.pieces

    def clear(self) -> None:
        self._entries.clear()
        self._pieces = 0


_CACHE = _RepresentationCache(_PIECE_BUDGET)


async def resolve_session_audio(session: RecordingSession) -> SessionAudio | None:
    """The session's stitched-audio representation, or ``None`` when it has none.

    Reuses the cached representation while the session's segment set is
    unchanged, and rebuilds it otherwise. Raises
    :class:`~api.stitch.NotStitchable` when the segments cannot form one
    continuous WAV. No database connection is held past the row read, so the
    caller streams the body with none checked out.
    """
    async with DatabasePipe() as pipe:
        fingerprint = await pipe.segments.stitch_fingerprint(session.id)
        if fingerprint.count == 0:
            return None
        cached = _CACHE.get(session.id, fingerprint)
        if cached is not None:
            return cached
        segments = await pipe.segments.list_for_session(
            session.id, resource=Resource.AUDIO, limit=_MAX_STITCH_SEGMENTS
        )

    # Pure planning plus a 44-byte header fetch, all off the database connection.
    stitch.check_uniform(segments)
    plan = stitch.plan(segments)
    async with BlobPipe() as blobs:
        template = b"".join(
            [
                chunk
                async for chunk in blobs.stream(
                    segments[0].object_key, start=0, end=stitch.WAV_HEADER_BYTES - 1
                )
            ]
        )
    header = stitch.patch_header(template, plan.data_bytes)

    audio = SessionAudio(etag=_session_etag(segments), header=header, plan=plan)
    _CACHE.put(session.id, fingerprint, audio)
    return audio
