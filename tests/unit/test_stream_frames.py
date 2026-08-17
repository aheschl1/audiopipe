"""The binary chunk envelope and JSON frame parsing, no I/O involved."""

import pytest

from api.schema.stream import (
    ChunkHeader,
    Finish,
    FrameError,
    Hello,
    Rotate,
    decode_chunk,
    encode_chunk,
    parse_client_frame,
    parse_server_event,
)


def test_chunk_round_trip() -> None:
    header = ChunkHeader(
        sequence=7,
        duration_ms=200,
        content_type="audio/wav",
        checksum_sha256="ab" * 32,
        metadata={"take": 3},
    )
    payload = bytes(range(256)) * 4
    decoded_header, decoded_payload = decode_chunk(encode_chunk(header, payload))
    assert decoded_header == header
    assert decoded_payload == payload


def test_chunk_round_trip_empty_payload() -> None:
    # The envelope itself allows it; the server rejects it at ingest.
    _, payload = decode_chunk(encode_chunk(ChunkHeader(sequence=0), b""))
    assert payload == b""


def test_decode_rejects_truncated_prefix() -> None:
    with pytest.raises(FrameError):
        decode_chunk(b"\x00\x00")


def test_decode_rejects_header_length_past_frame() -> None:
    frame = (100).to_bytes(4, "big") + b'{"sequence": 0}'
    with pytest.raises(FrameError):
        decode_chunk(frame)


def test_decode_rejects_header_that_is_not_json() -> None:
    head = b"not json at all"
    with pytest.raises(FrameError):
        decode_chunk(len(head).to_bytes(4, "big") + head + b"payload")


def test_decode_rejects_header_without_sequence() -> None:
    head = b'{"duration_ms": 100}'
    with pytest.raises(FrameError):
        decode_chunk(len(head).to_bytes(4, "big") + head + b"payload")


def test_header_ignores_unknown_fields() -> None:
    # Forward compatibility: an older server must accept a newer client's header.
    head = b'{"sequence": 3, "some_future_field": true}'
    header, payload = decode_chunk(len(head).to_bytes(4, "big") + head + b"x")
    assert header.sequence == 3
    assert payload == b"x"


def test_header_without_resource_is_audio() -> None:
    # Every pre-resource client remains a valid audio stream.
    head = b'{"sequence": 0}'
    header, _ = decode_chunk(len(head).to_bytes(4, "big") + head + b"x")
    assert header.resource == "audio"


def test_header_carries_resource() -> None:
    header = ChunkHeader(sequence=4, resource="location")
    decoded, _ = decode_chunk(encode_chunk(header, b'{"points": []}'))
    assert decoded.resource == "location"


def test_parse_client_frame_hello_and_finish() -> None:
    hello = parse_client_frame(
        '{"type": "hello", "version": 1, "defaults": {"content_type": "audio/wav"}}'
    )
    assert isinstance(hello, Hello)
    assert hello.defaults.content_type == "audio/wav"
    assert hello.token is None

    finish = parse_client_frame('{"type": "finish"}')
    assert isinstance(finish, Finish)
    assert finish.ended_at is None


def test_parse_hello_with_token() -> None:
    # Hello-token mode: the fallback credential for header-less clients.
    hello = parse_client_frame('{"type": "hello", "version": 1, "token": "tok-123"}')
    assert isinstance(hello, Hello)
    assert hello.token == "tok-123"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json",
        '{"type": "unknown"}',
        '{"version": 1}',  # no type
        '{"type": "hello"}',  # no version
    ],
)
def test_parse_client_frame_rejects_invalid(text: str) -> None:
    with pytest.raises(FrameError):
        parse_client_frame(text)


def test_rotate_round_trip() -> None:
    event = parse_server_event(Rotate(through=12).model_dump_json())
    assert isinstance(event, Rotate)
    assert event.through == 12
