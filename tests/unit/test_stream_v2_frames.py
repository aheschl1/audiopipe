"""The v2 binary frame codec, no I/O involved."""

from datetime import UTC, datetime

import pytest

from api.schema.stream import (
    FRAME_ENVELOPE,
    AudioFrame,
    ChunkHeader,
    FrameError,
    decode_v2_frame,
    encode_audio_frame,
    encode_chunk,
)


def test_audio_frame_round_trip() -> None:
    frame = decode_v2_frame(encode_audio_frame(7, b"\x01\x02" * 100))
    assert isinstance(frame, AudioFrame)
    assert frame.sequence == 7
    assert frame.captured_at is None
    assert frame.pcm == b"\x01\x02" * 100


def test_audio_frame_round_trip_with_captured_at() -> None:
    stamp = datetime(2026, 8, 17, 12, 0, 0, 250_000, tzinfo=UTC)
    frame = decode_v2_frame(encode_audio_frame(0, b"\x00\x00", captured_at=stamp))
    assert isinstance(frame, AudioFrame)
    assert frame.captured_at == stamp


def test_envelope_frame_round_trip() -> None:
    inner = encode_chunk(ChunkHeader(sequence=3, resource="location"), b'{"points": []}')
    decoded = decode_v2_frame(bytes((FRAME_ENVELOPE,)) + inner)
    assert isinstance(decoded, tuple)
    header, payload = decoded
    assert header.sequence == 3
    assert header.resource == "location"
    assert payload == b'{"points": []}'


def test_empty_audio_payload_decodes() -> None:
    # The codec allows it; the server rejects it at ingest.
    frame = decode_v2_frame(encode_audio_frame(1, b""))
    assert isinstance(frame, AudioFrame)
    assert frame.pcm == b""


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\x7f" + b"x" * 10,  # unknown discriminator
        b"\x01\x00\x00\x00",  # shorter than the fixed header
        b"\x01\x80" + (5).to_bytes(4, "big") + b"pcm",  # unknown flag bit
        b"\x01\x01" + (5).to_bytes(4, "big") + b"\x00" * 4,  # truncated stamp
        b"\x02\x00\x00",  # envelope shorter than its length prefix
    ],
)
def test_decode_rejects_malformed(data: bytes) -> None:
    with pytest.raises(FrameError):
        decode_v2_frame(data)
