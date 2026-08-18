"""Pooling of v2 audio frames into stored WAV segments.

Fast raw-PCM frames are appended in arrival order (the v2 ordering rule makes
concatenation safe) and flushed as one canonical WAV segment when the pool
reaches its target size or its oldest frame reaches the latency ceiling. A
stored row's ``sequence`` is the LAST frame sequence it covers, which keeps
``MAX(sequence)+1`` — the resume point — correct with no schema changes.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime

from api.config import ApiSettings
from api.schema.stream import AudioFrame, StreamError
from database.exceptions import DuplicateSequenceError, NotFoundError, SessionEndedError
from database.schema.sessions import RecordingSession
from services.segments import stream_segment
from services.wav import pcm_duration_ms, wrap_pcm
from storage.s3 import S3BlobStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _Flush:
    """One pool snapshot on its way to storage."""

    pcm: bytes
    frames: tuple[tuple[int, int], ...]  # (sequence, byte_size) per frame
    captured_at: datetime | None


class PcmPooler:
    """Accumulates one connection's audio frames and stores them pooled.

    ``add`` is called inline by the frame pump; it only awaits when the
    buffered-but-not-durable total exceeds the configured cap, so storage
    outages become TCP backpressure instead of unbounded memory. Stores run
    through the connection's ``_ChunkPool`` and report acks per covered frame.
    """

    def __init__(
        self,
        blobs: S3BlobStore,
        session: RecordingSession,
        *,
        sample_rate_hz: int,
        channels: int,
        tracker,
        chunk_pool,
        outbox,
        settings: ApiSettings,
    ) -> None:
        self._blobs = blobs
        self._session = session
        self._sample_rate_hz = sample_rate_hz
        self._channels = channels
        self._tracker = tracker
        self._chunk_pool = chunk_pool
        self._outbox = outbox
        self._settings = settings
        self._buffer = bytearray()
        self._frames: list[tuple[int, int]] = []
        self._captured_at: datetime | None = None
        self._pending = 0  # bytes submitted but not yet durable
        self._room = asyncio.Event()
        self._room.set()
        self._timer: asyncio.Task | None = None
        self.failed = False
        # The v2 ordering rule's watermark: starts at the resume point, so a
        # reconnect's first frame must top everything already stored.
        self.highest_sequence = tracker.through

    async def add(self, frame: AudioFrame) -> None:
        """Append one frame; flush and/or block when thresholds say so."""
        if self._captured_at is None and not self._frames:
            self._captured_at = frame.captured_at
        self.highest_sequence = frame.sequence
        self._frames.append((frame.sequence, len(frame.pcm)))
        self._buffer.extend(frame.pcm)
        if len(self._buffer) >= self._settings.stream_pool_target_bytes:
            await self._submit()
        elif self._timer is None:
            self._timer = asyncio.create_task(self._flush_later())
        while len(self._buffer) + self._pending > self._settings.stream_pool_max_buffer_bytes:
            self._room.clear()
            await self._room.wait()

    async def flush(self) -> None:
        """Submit whatever is pooled; completion lands via the chunk pool."""
        if self._frames:
            await self._submit()

    def shutdown(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    async def _flush_later(self) -> None:
        await asyncio.sleep(self._settings.stream_pool_max_latency_seconds)
        self._timer = None
        if self._frames:
            await self._submit()

    async def _submit(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        flush = _Flush(
            pcm=bytes(self._buffer), frames=tuple(self._frames), captured_at=self._captured_at
        )
        self._buffer.clear()
        self._frames.clear()
        self._captured_at = None
        self._pending += len(flush.pcm)
        await self._chunk_pool.submit(lambda: self._store(flush))

    async def _store(self, flush: _Flush) -> None:
        try:
            await self._store_with_retries(flush)
        finally:
            self._pending -= len(flush.pcm)
            self._room.set()

    async def _store_with_retries(self, flush: _Flush) -> None:
        data = wrap_pcm(flush.pcm, self._sample_rate_hz, self._channels)
        first, last = flush.frames[0][0], flush.frames[-1][0]
        backoff = self._settings.stream_pool_retry_backoff_seconds
        for attempts_left in reversed(range(self._settings.stream_pool_flush_retries + 1)):
            try:
                await stream_segment(
                    self._blobs,
                    self._session,
                    last,
                    data,
                    content_type="audio/wav",
                    captured_at=flush.captured_at,
                    duration_ms=pcm_duration_ms(
                        len(flush.pcm), self._sample_rate_hz, self._channels
                    ),
                    attrs={
                        "codec": "pcm_s16le",
                        "sample_rate_hz": self._sample_rate_hz,
                        "channels": self._channels,
                    },
                    metadata={"frames": {"first": first, "last": last, "count": len(flush.frames)}},
                )
            except DuplicateSequenceError:
                # A reconnect raced its predecessor's flush; the data is stored.
                for sequence, _ in flush.frames:
                    self._tracker.note_duplicate(sequence)
                return
            except (SessionEndedError, NotFoundError):
                raise  # the chunk pool's fatal path; unacked frames retransmit
            except Exception:
                if attempts_left == 0:
                    logger.exception(
                        "pooled flush %d..%d of session %s failed; giving up",
                        first, last, self._session.id,
                    )
                    self.failed = True
                    self._outbox.publish(
                        StreamError(
                            scope="session",
                            code="storage_failure",
                            detail="pooled audio could not be stored; reconnect and retransmit",
                        )
                    )
                    return
                await asyncio.sleep(backoff)
                backoff *= 2
                continue
            for sequence, byte_size in flush.frames:
                self._tracker.note_stored(sequence, byte_size)
            return
