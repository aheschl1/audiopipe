"""Segment ingest and deletion for every resource type.

Blob-stored resources (audio) touch both stores, which cannot share a
transaction, so the ordering lives here:

* **ingest** writes the object, then the row; a failed row deletes the object.
* **delete** removes the row, then the object.

Inline resources (location) never touch the blob store: the validated payload
is part of the row, so ingest is a single transaction and needs no
compensation.

Every chunk passes its resource type's validator (:mod:`resources`) before
anything is written.
"""

import hashlib
from datetime import datetime
from typing import Any, Mapping
from uuid import UUID, uuid4

import resources
from database.exceptions import (
    DuplicateSequenceError,
    NotFoundError,
    SessionEndedError,
)
from database.pipe import DatabasePipe
from database.schema.segments import ResourceSegment, SegmentCreate
from database.schema.sessions import RecordingSession
from resources import Resource
from resources.base import ValidatedChunk
from storage.keys import segment_key, session_prefix, suffix_for
from storage.pipe import BlobPipe
from storage.s3 import S3BlobStore


class ChecksumMismatchError(Exception):
    """The caller-declared checksum does not match the received bytes."""


class NoBlobError(Exception):
    """The segment stores its payload inline; there is no object to serve."""


# Concurrent ingests into one session race for the same sequence number; the
# loser retries with a fresh read. N concurrent callers need at most N
# attempts (each round commits at least one), so this bounds contention width.
_SEQUENCE_RETRIES = 5


async def ingest_segment(
    session_id: UUID,
    data: bytes,
    *,
    resource: str = Resource.AUDIO,
    content_type: str | None = None,
    filename: str | None = None,
    sequence: int | None = None,
    captured_at: datetime | None = None,
    offset_ms: int | None = None,
    duration_ms: int | None = None,
    attrs: Mapping[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> ResourceSegment:
    """Store one segment's bytes and register it against its session.

    Nothing is locked across a blob upload: the sequence number is a
    snapshot read committed before the PUT, and the ``(session_id, sequence)``
    unique constraint arbitrates concurrent ingests — a loser deletes its
    object and retries with a fresh number. A caller-supplied ``sequence``
    keeps single-shot semantics: its duplicate is a real error, as on the
    streaming path.

    Raises :class:`resources.ResourceValidationError` (before anything is
    written) for a payload the resource type rejects, and KeyError for an
    unknown ``resource``.
    """
    validated = resources.get(resource).validate_chunk(
        data,
        content_type=content_type,
        declared_attrs=attrs or {},
        captured_at=captured_at,
        duration_ms=duration_ms,
    )
    checksum = hashlib.sha256(data).digest()
    for attempts_left in reversed(range(_SEQUENCE_RETRIES)):
        try:
            return await _ingest_once(
                session_id,
                data,
                resource=resource,
                validated=validated,
                checksum=checksum,
                filename=filename,
                sequence=sequence,
                offset_ms=offset_ms,
                metadata=metadata,
            )
        except DuplicateSequenceError:
            if sequence is not None or attempts_left == 0:
                raise
    raise AssertionError("unreachable")  # the loop returns or raises


async def _ingest_once(
    session_id: UUID,
    data: bytes,
    *,
    resource: str,
    validated: ValidatedChunk,
    checksum: bytes,
    filename: str | None,
    sequence: int | None,
    offset_ms: int | None,
    metadata: dict[str, Any] | None,
) -> ResourceSegment:
    segment_id = uuid4()
    async with DatabasePipe() as pipe:
        session = await pipe.sessions.get(session_id)
        if session is None:
            raise NotFoundError("recording session", session_id)
        if sequence is None:
            sequence = await pipe.segments.next_sequence(session_id)
        if validated.payload is not None:
            # Inline: the row is the storage — one transaction, done.
            await pipe.sessions.touch(session_id)
            return await pipe.segments.create(
                _row(session, segment_id, sequence, resource, validated, checksum,
                     byte_size=len(data), offset_ms=offset_ms, metadata=metadata)
            )

    key = segment_key(
        session.user_id,
        session_id,
        segment_id,
        sequence,
        suffix_for(validated.content_type, filename),
    )
    async with BlobPipe() as blobs:
        info = await blobs.put(key, data, content_type=validated.content_type)
        try:
            async with DatabasePipe() as pipe:
                await pipe.sessions.touch(session_id)
                return await pipe.segments.create(
                    _row(session, segment_id, sequence, resource, validated, checksum,
                         byte_size=info.byte_size, offset_ms=offset_ms, metadata=metadata,
                         bucket=info.bucket, object_key=info.key)
                )
        except BaseException:
            # Without a row, nothing will reference this object again. A
            # failure of the COMMIT itself still orphans it.
            await blobs.delete(key)
            raise


async def stream_segment(
    blobs: S3BlobStore,
    session: RecordingSession,
    sequence: int,
    data: bytes,
    *,
    resource: str = Resource.AUDIO,
    content_type: str | None = None,
    captured_at: datetime | None = None,
    duration_ms: int | None = None,
    attrs: Mapping[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    expected_checksum: bytes | None = None,
) -> ResourceSegment:
    """Store one streamed chunk against an already-open blob store.

    The websocket ingest path: the caller holds ``blobs`` for its whole
    connection and supplies the sequence number, so unlike :func:`ingest_segment`
    no client is built and no session lock is taken per chunk. The database
    transaction spans only the row insert. Inline resources skip ``blobs``
    entirely.

    Raises :class:`ChecksumMismatchError` and
    :class:`resources.ResourceValidationError` before anything is written,
    :class:`~database.exceptions.SessionEndedError` when the session was ended
    since the caller loaded it, and
    :class:`~database.exceptions.DuplicateSequenceError` for a retransmit of a
    stored sequence (the original row and object are untouched).
    """
    checksum = hashlib.sha256(data).digest()
    if expected_checksum is not None and expected_checksum != checksum:
        raise ChecksumMismatchError(
            f"declared sha256 {expected_checksum.hex()} != received {checksum.hex()}"
        )
    validated = resources.get(resource).validate_chunk(
        data,
        content_type=content_type,
        declared_attrs=attrs or {},
        captured_at=captured_at,
        duration_ms=duration_ms,
    )

    segment_id = uuid4()
    if validated.payload is not None:
        async with DatabasePipe() as pipe:
            if not await pipe.sessions.touch_if_stale(session.id):
                raise SessionEndedError(session.id)
            return await pipe.segments.create(
                _row(session, segment_id, sequence, resource, validated, checksum,
                     byte_size=len(data), metadata=metadata)
            )

    key = segment_key(
        session.user_id, session.id, segment_id, sequence, suffix_for(validated.content_type)
    )
    info = await blobs.put(key, data, content_type=validated.content_type)
    try:
        async with DatabasePipe() as pipe:
            # Doubles as the liveness check: False means the session ended
            # (REST or sweeper) after this connection attached. Throttled — a
            # chunk-per-second stream must not rewrite the session row (row
            # lock, WAL, trigger) on every chunk.
            if not await pipe.sessions.touch_if_stale(session.id):
                raise SessionEndedError(session.id)
            return await pipe.segments.create(
                _row(session, segment_id, sequence, resource, validated, checksum,
                     byte_size=info.byte_size, metadata=metadata,
                     bucket=info.bucket, object_key=info.key)
            )
    except BaseException:
        await blobs.delete(key)
        raise


def _row(
    session: RecordingSession,
    segment_id: UUID,
    sequence: int,
    resource: str,
    validated: ValidatedChunk,
    checksum: bytes,
    *,
    byte_size: int,
    metadata: dict[str, Any] | None,
    offset_ms: int | None = None,
    bucket: str | None = None,
    object_key: str | None = None,
) -> SegmentCreate:
    return SegmentCreate(
        id=segment_id,
        session_id=session.id,
        user_id=session.user_id,
        resource=resource,
        sequence=sequence,
        bucket=bucket,
        object_key=object_key,
        payload=validated.payload,
        byte_size=byte_size,
        content_type=validated.content_type,
        checksum_sha256=checksum,
        captured_at=validated.captured_at,
        offset_ms=offset_ms,
        duration_ms=validated.duration_ms,
        attrs=validated.attrs,
        metadata=metadata or {},
    )


async def read_segment(segment_id: UUID) -> tuple[ResourceSegment, bytes]:
    """Fetch a blob-backed segment's metadata and its bytes."""
    segment = await _require_segment(segment_id)
    if segment.object_key is None:
        raise NoBlobError(f"segment {segment_id} stores its payload inline")
    async with BlobPipe() as blobs:
        return segment, await blobs.get(segment.object_key)


async def segment_url(segment_id: UUID, expires_in: int | None = None) -> str:
    """A presigned URL that serves a blob-backed segment's bytes directly."""
    segment = await _require_segment(segment_id)
    if segment.object_key is None:
        raise NoBlobError(f"segment {segment_id} stores its payload inline")
    async with BlobPipe() as blobs:
        return await blobs.presign_get(segment.object_key, expires_in=expires_in)


async def delete_segment(segment_id: UUID) -> bool:
    """Delete one segment: its row, then its object (if it has one)."""
    async with DatabasePipe() as pipe:
        segment = await pipe.segments.get(segment_id)
        if segment is None:
            return False
        await pipe.segments.delete(segment_id)
    if segment.object_key is not None:
        async with BlobPipe() as blobs:
            await blobs.delete(segment.object_key)
    return True


async def delete_session(session_id: UUID) -> int:
    """Delete a session, its segment rows (by cascade), and all of its objects.

    Returns the number of objects removed.
    """
    async with DatabasePipe() as pipe:
        session = await pipe.sessions.get(session_id)
        if session is None:
            raise NotFoundError("recording session", session_id)
        await pipe.sessions.delete(session_id)
    async with BlobPipe() as blobs:
        return await blobs.delete_prefix(session_prefix(session.user_id, session_id))


async def _require_segment(segment_id: UUID) -> ResourceSegment:
    async with DatabasePipe() as pipe:
        segment = await pipe.segments.get(segment_id)
    if segment is None:
        raise NotFoundError("segment", segment_id)
    return segment
