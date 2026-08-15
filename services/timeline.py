"""The session timeline: audio addressed as milliseconds from session start.

Processing tiers attach artifacts to *time ranges of a session* — the segment
is an implementation detail. For a uniform-WAV session the stitched PCM stream
(``services.stitch``) is a continuous timeline: time maps linearly to a byte
offset in the stitched data area. This module loads that mapping and renders
any range as a standalone WAV clip for model input, or walks the whole session
as fixed windows for scanning passes (VAD).

Non-WAV sessions are not renderable yet (:class:`NotRenderable`); an ffmpeg
decode fallback is the future path once clients move to Opus.
"""

import struct
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

from database.pipe import DatabasePipe
from database.schema.segments import ResourceSegment
from resources import Resource
from resources.audio import AudioAttrs
from services import stitch
from storage.pipe import BlobPipe

# Far past any real session (one chunk per second for a day is 86 400).
_MAX_SEGMENTS = 1_000_000

_BYTES_PER_SAMPLE = 2  # 16-bit PCM only


class NotRenderable(Exception):
    """The session's audio cannot be placed on a PCM timeline."""


@dataclass(frozen=True, slots=True)
class SessionTimeline:
    """The continuous-PCM view of one session's audio."""

    session_id: UUID
    plan: stitch.StitchPlan
    sample_rate_hz: int
    channels: int

    @property
    def frame_bytes(self) -> int:
        return self.channels * _BYTES_PER_SAMPLE

    @property
    def total_ms(self) -> int:
        return self.plan.data_bytes * 1000 // (self.sample_rate_hz * self.frame_bytes)

    def _frame_offset(self, ms: int) -> int:
        """Stitched-file byte offset of the frame at ``ms``, frame-aligned."""
        frames = ms * self.sample_rate_hz // 1000
        return stitch.WAV_HEADER_BYTES + frames * self.frame_bytes

    def byte_range(self, start_ms: int, end_ms: int) -> tuple[int, int]:
        """Inclusive stitched-file byte range for ``[start_ms, end_ms)``,
        clamped to the audio that exists."""
        start = self._frame_offset(max(0, start_ms))
        end = min(self._frame_offset(end_ms), stitch.WAV_HEADER_BYTES + self.plan.data_bytes)
        if end <= start:
            raise NotRenderable(f"empty range {start_ms}..{end_ms}ms")
        return start, end - 1


def _parse_wav_fmt(header: bytes) -> tuple[int, int]:
    """(sample_rate, channels) from a canonical 44-byte WAV header."""
    if len(header) < stitch.WAV_HEADER_BYTES or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
        raise NotRenderable("first segment does not start with a canonical WAV header")
    channels, rate = struct.unpack_from("<HI", header, 22)
    (bits,) = struct.unpack_from("<H", header, 34)
    if bits != 16:
        raise NotRenderable(f"only 16-bit PCM is renderable, segment is {bits}-bit")
    return rate, channels


def wav_header(data_bytes: int, sample_rate_hz: int, channels: int) -> bytes:
    """A canonical 44-byte PCM WAV header for a rendered clip."""
    byte_rate = sample_rate_hz * channels * _BYTES_PER_SAMPLE
    return (
        b"RIFF"
        + struct.pack("<I", 36 + data_bytes)
        + b"WAVEfmt "
        + struct.pack(
            "<IHHIIHH",
            16, 1, channels, sample_rate_hz, byte_rate,
            channels * _BYTES_PER_SAMPLE, 16,
        )
        + b"data"
        + struct.pack("<I", data_bytes)
    )


async def load_timeline(session_id: UUID) -> SessionTimeline | None:
    """Build the timeline for a session, or None when it holds no audio.

    Raises :class:`~services.stitch.NotStitchable` for mixed formats and
    :class:`NotRenderable` when the PCM parameters cannot be determined.
    Sample rate and channels come from the segment rows when the client
    declared them, else from the first segment's WAV header.
    """
    async with DatabasePipe() as pipe:
        segments = await pipe.segments.list_for_session(
            session_id, resource=Resource.AUDIO, limit=_MAX_SEGMENTS
        )
    if not segments:
        return None
    stitch.check_uniform(segments)
    rate, channels = await _pcm_parameters(segments)
    return SessionTimeline(
        session_id=session_id,
        plan=stitch.plan(segments),
        sample_rate_hz=rate,
        channels=channels,
    )


async def _pcm_parameters(segments: list[ResourceSegment]) -> tuple[int, int]:
    first = segments[0]
    attrs = AudioAttrs.from_attrs(first.attrs)
    if attrs.sample_rate_hz and attrs.channels:
        return attrs.sample_rate_hz, attrs.channels
    assert first.object_key is not None  # audio/wav (check_uniform) is blob-backed
    async with BlobPipe() as blobs:
        header = b"".join(
            [
                chunk
                async for chunk in blobs.stream(
                    first.object_key, start=0, end=stitch.WAV_HEADER_BYTES - 1
                )
            ]
        )
    return _parse_wav_fmt(header)


async def render_range(timeline: SessionTimeline, start_ms: int, end_ms: int) -> bytes:
    """The range ``[start_ms, end_ms)`` as one standalone WAV clip.

    Ranged blob reads: only the requested slice crosses the network, so a
    short span of a long session stays cheap.
    """
    start, end = timeline.byte_range(start_ms, end_ms)
    body = bytearray()
    async with BlobPipe() as blobs:
        for piece, piece_start, piece_end in stitch.slices(timeline.plan, start, end):
            assert piece is not None  # start >= WAV_HEADER_BYTES: never the header slice
            async for chunk in blobs.stream(piece.object_key, start=piece_start, end=piece_end):
                body += chunk
    return wav_header(len(body), timeline.sample_rate_hz, timeline.channels) + bytes(body)


async def iter_windows(
    timeline: SessionTimeline, window_ms: int
) -> AsyncIterator[tuple[int, bytes]]:
    """Yield ``(start_ms, pcm)`` windows covering the whole session, in order.

    One sequential read per segment (a scanning pass touches everything
    anyway); the trailing partial window is yielded last. ``pcm`` is raw
    frames, no WAV header.
    """
    window_bytes = (
        window_ms * timeline.sample_rate_hz // 1000
    ) * timeline.frame_bytes
    buffer = bytearray()
    emitted = 0
    async with BlobPipe() as blobs:
        for piece in timeline.plan.pieces:
            async for chunk in blobs.stream(
                piece.object_key,
                start=stitch.WAV_HEADER_BYTES,
                end=stitch.WAV_HEADER_BYTES + piece.data_length - 1,
            ):
                buffer += chunk
                while len(buffer) >= window_bytes:
                    yield emitted * window_ms, bytes(buffer[:window_bytes])
                    del buffer[:window_bytes]
                    emitted += 1
    if buffer:
        yield emitted * window_ms, bytes(buffer)
