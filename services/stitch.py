"""Stitch a session's stored WAV segments into one logical file.

Segments arrive as self-contained canonical WAVs — a 44-byte header followed
by PCM data, the shape the streaming client uploads. A session's audio is
therefore their data chunks concatenated behind a single header whose RIFF and
data sizes are patched to the combined length. Everything here is pure
planning over segment rows; the route streams blob ranges against the plan.
"""

import struct
from dataclasses import dataclass
from typing import Iterator

from database.schema.segments import ResourceSegment
from resources.audio import AudioAttrs

WAV_HEADER_BYTES = 44

_RIFF_SIZE_OFFSET = 4
_DATA_SIZE_OFFSET = 40


class NotStitchable(Exception):
    """The session's segments cannot form one continuous WAV."""


@dataclass(frozen=True, slots=True)
class Piece:
    """One segment's data chunk, placed in the stitched file."""

    object_key: str
    start: int  # offset of this piece's first byte in the stitched file
    data_length: int

    @property
    def end(self) -> int:
        return self.start + self.data_length - 1


@dataclass(frozen=True, slots=True)
class StitchPlan:
    pieces: tuple[Piece, ...]
    total_size: int  # header plus every piece

    @property
    def data_bytes(self) -> int:
        return self.total_size - WAV_HEADER_BYTES


def check_uniform(segments: list[ResourceSegment]) -> None:
    """Reject mixed formats before any byte is sent.

    Callers pass audio segments only; other resources have no PCM stream to
    join.
    """
    types = {segment.content_type for segment in segments}
    if types != {"audio/wav"}:
        raise NotStitchable(f"session mixes content types {sorted(types)}; only audio/wav stitches")
    attrs = [AudioAttrs.from_attrs(segment.attrs) for segment in segments]
    for field in ("sample_rate_hz", "channels", "codec"):
        values = {getattr(item, field) for item in attrs} - {None}
        if len(values) > 1:
            raise NotStitchable(f"session mixes {field} values {sorted(values)}")


def plan(segments: list[ResourceSegment]) -> StitchPlan:
    """Lay the segments' data chunks out behind one shared header.

    A segment shorter than its own header contributes nothing rather than
    corrupting the stream.
    """
    pieces: list[Piece] = []
    at = WAV_HEADER_BYTES
    for segment in segments:
        length = max(0, segment.byte_size - WAV_HEADER_BYTES)
        if length == 0:
            continue
        assert segment.object_key is not None  # audio/wav (check_uniform) is blob-backed
        pieces.append(Piece(object_key=segment.object_key, start=at, data_length=length))
        at += length
    return StitchPlan(pieces=tuple(pieces), total_size=at)


def patch_header(template: bytes, data_bytes: int) -> bytes:
    """The first segment's header, its two size fields rewritten."""
    if len(template) < WAV_HEADER_BYTES:
        raise NotStitchable("first segment is shorter than a WAV header")
    if template[:4] != b"RIFF" or template[8:12] != b"WAVE":
        raise NotStitchable("first segment does not start with a WAV header")
    header = bytearray(template[:WAV_HEADER_BYTES])
    header[_RIFF_SIZE_OFFSET : _RIFF_SIZE_OFFSET + 4] = struct.pack("<I", 36 + data_bytes)
    header[_DATA_SIZE_OFFSET : _DATA_SIZE_OFFSET + 4] = struct.pack("<I", data_bytes)
    return bytes(header)


# What slices() yields: a piece of the patched header, or a byte range within
# one blob (offsets relative to the blob, i.e. past its own 44-byte header).
HeaderSlice = tuple[None, int, int]
BlobSlice = tuple[Piece, int, int]


def slices(stitch_plan: StitchPlan, start: int, end: int) -> Iterator[HeaderSlice | BlobSlice]:
    """Map an inclusive stitched-file range onto header and blob ranges."""
    if start < WAV_HEADER_BYTES:
        yield None, start, min(end, WAV_HEADER_BYTES - 1)
    for piece in stitch_plan.pieces:
        if piece.end < start or piece.start > end:
            continue
        local_start = max(start, piece.start) - piece.start
        local_end = min(end, piece.end) - piece.start
        yield piece, WAV_HEADER_BYTES + local_start, WAV_HEADER_BYTES + local_end
