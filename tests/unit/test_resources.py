"""The resource registry and its per-type chunk validators, pure and fast."""

import json
from datetime import UTC, datetime

import pytest

import resources
from resources import ResourceValidationError
from resources.location import MAX_POINTS_PER_BATCH


def _location_bytes(points: list[dict]) -> bytes:
    return json.dumps({"points": points}).encode()


def _point(t: int, **overrides) -> dict:
    return {"lat": 51.0, "lon": -114.0, "t": t, **overrides}


def test_registry_names_and_lookup() -> None:
    assert resources.names() == ("audio", "location")
    assert resources.get("audio").storage == "blob"
    assert resources.get("location").storage == "inline"
    with pytest.raises(KeyError):
        resources.get("video")


def test_audio_accepts_any_bytes_and_normalizes_attrs() -> None:
    chunk = resources.get("audio").validate_chunk(
        b"\x00garbage",
        content_type=None,
        declared_attrs={"codec": None, "sample_rate_hz": 16000, "channels": 1},
        captured_at=None,
        duration_ms=250,
    )
    assert chunk.payload is None
    assert chunk.content_type == "application/octet-stream"
    assert chunk.attrs == {"sample_rate_hz": 16000, "channels": 1}
    assert chunk.duration_ms == 250


def test_audio_rejects_malformed_attrs() -> None:
    with pytest.raises(ResourceValidationError):
        resources.get("audio").validate_chunk(
            b"x",
            content_type=None,
            declared_attrs={"sample_rate_hz": "very fast"},
            captured_at=None,
            duration_ms=None,
        )


def test_location_parses_batch_and_derives_span() -> None:
    chunk = resources.get("location").validate_chunk(
        _location_bytes([_point(1_000), _point(4_000, alt_m=1045.0, accuracy_m=8.5)]),
        content_type=None,
        declared_attrs={},
        captured_at=None,
        duration_ms=None,
    )
    assert chunk.content_type == "application/json"
    assert chunk.captured_at == datetime.fromtimestamp(1.0, tz=UTC)
    assert chunk.duration_ms == 3_000
    assert chunk.payload == {
        "points": [
            {"lat": 51.0, "lon": -114.0, "t": 1_000},
            {"lat": 51.0, "lon": -114.0, "t": 4_000, "alt_m": 1045.0, "accuracy_m": 8.5},
        ]
    }


def test_location_keeps_declared_capture_and_span() -> None:
    declared = datetime(2026, 8, 14, tzinfo=UTC)
    chunk = resources.get("location").validate_chunk(
        _location_bytes([_point(1_000)]),
        content_type="application/json",
        declared_attrs={},
        captured_at=declared,
        duration_ms=0,
    )
    assert chunk.captured_at == declared
    assert chunk.duration_ms == 0


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        b'{"points": []}',
        _location_bytes([_point(1, lat=91.0)]),
        _location_bytes([_point(1, lon=-181.0)]),
        _location_bytes([_point(5), _point(3)]),  # out of order
        _location_bytes([_point(1, accuracy_m=-1.0)]),
        _location_bytes([_point(t) for t in range(MAX_POINTS_PER_BATCH + 1)]),
    ],
)
def test_location_rejects_bad_batches(payload: bytes) -> None:
    with pytest.raises(ResourceValidationError):
        resources.get("location").validate_chunk(
            payload,
            content_type=None,
            declared_attrs={},
            captured_at=None,
            duration_ms=None,
        )


def test_location_rejects_wrong_content_type_and_attrs() -> None:
    good = _location_bytes([_point(1)])
    with pytest.raises(ResourceValidationError):
        resources.get("location").validate_chunk(
            good, content_type="audio/wav", declared_attrs={},
            captured_at=None, duration_ms=None,
        )
    with pytest.raises(ResourceValidationError):
        resources.get("location").validate_chunk(
            good, content_type=None, declared_attrs={"codec": "gps"},
            captured_at=None, duration_ms=None,
        )
