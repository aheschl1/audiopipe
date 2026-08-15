"""Stitch planning and range mapping, pure and Docker-free."""

import struct
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from services.stitch import (
    WAV_HEADER_BYTES,
    NotStitchable,
    check_uniform,
    patch_header,
    plan,
    slices,
)
from database.schema.segments import ResourceSegment
from tests.wav import wav_bytes


def segment(byte_size: int, content_type: str = "audio/wav", **overrides) -> ResourceSegment:
    attrs = {
        key: overrides.pop(key)
        for key in ("codec", "sample_rate_hz", "channels")
        if key in overrides
    }
    return ResourceSegment(
        id=uuid4(),
        session_id=uuid4(),
        user_id=uuid4(),
        resource="audio",
        sequence=overrides.pop("sequence", 0),
        ingested_at=datetime.now(UTC),
        bucket="test",
        object_key=f"k/{uuid4()}",
        byte_size=byte_size,
        content_type=content_type,
        attrs=attrs,
        **overrides,
    )


def test_plan_lays_pieces_end_to_end() -> None:
    p = plan([segment(144), segment(244), segment(44), segment(544)])
    # 44-byte segment contributes no data and is skipped entirely.
    assert [piece.data_length for piece in p.pieces] == [100, 200, 500]
    assert [piece.start for piece in p.pieces] == [44, 144, 344]
    assert p.total_size == 44 + 800
    assert p.data_bytes == 800


def test_patch_header_rewrites_both_size_fields() -> None:
    template = wav_bytes(seconds=0.01)[:WAV_HEADER_BYTES]
    patched = patch_header(template, 800)
    assert patched[:4] == b"RIFF"
    assert struct.unpack("<I", patched[4:8])[0] == 36 + 800
    assert patched[8:40] == template[8:40]  # fmt chunk untouched
    assert struct.unpack("<I", patched[40:44])[0] == 800


def test_patch_header_rejects_non_wav() -> None:
    with pytest.raises(NotStitchable):
        patch_header(b"\x00" * WAV_HEADER_BYTES, 10)
    with pytest.raises(NotStitchable):
        patch_header(b"RIFF", 10)  # too short


def test_check_uniform() -> None:
    check_uniform([segment(100), segment(100, sample_rate_hz=16000)])
    with pytest.raises(NotStitchable):
        check_uniform([segment(100), segment(100, content_type="audio/webm")])
    with pytest.raises(NotStitchable):
        check_uniform(
            [segment(100, sample_rate_hz=16000), segment(100, sample_rate_hz=48000)]
        )


def test_slices_full_range_covers_header_and_all_pieces() -> None:
    p = plan([segment(144), segment(244)])
    out = list(slices(p, 0, p.total_size - 1))
    assert out[0] == (None, 0, 43)
    assert (out[1][1], out[1][2]) == (44, 143)  # whole first blob's data
    assert (out[2][1], out[2][2]) == (44, 243)  # whole second blob's data


def test_slices_mid_range_spanning_a_boundary() -> None:
    p = plan([segment(144), segment(244)])
    # Stitched bytes 100..199: tail of piece 0 (data 56..99) + head of piece 1.
    out = list(slices(p, 100, 199))
    assert len(out) == 2
    piece0, start0, end0 = out[0]
    assert piece0 is not None and (start0, end0) == (44 + 56, 44 + 99)
    piece1, start1, end1 = out[1]
    assert piece1 is not None and (start1, end1) == (44 + 0, 44 + 55)


def test_slices_header_only() -> None:
    p = plan([segment(144)])
    assert list(slices(p, 0, 43)) == [(None, 0, 43)]
