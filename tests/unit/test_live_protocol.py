"""The UDS message codec, against in-memory stream readers."""

import asyncio
from uuid import uuid4

import pytest

from live.protocol import (
    MSG_AUDIO,
    MSG_EVENT,
    MSG_HELLO,
    Event,
    ProtocolError,
    SessionHello,
    decode_audio,
    decode_json,
    encode_audio,
    encode_message,
    read_message,
)


def _reader(data: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


async def test_message_roundtrip() -> None:
    hello = SessionHello(
        version=1,
        session_id=uuid4(),
        user_id=uuid4(),
        sample_rate_hz=16000,
        channels=1,
        effects=("live-counter",),
    )
    stream = (
        encode_message(MSG_HELLO, hello.model_dump_json().encode())
        + encode_audio(7, b"pcm-bytes")
        + encode_message(MSG_EVENT, Event(effect="e", event="started").model_dump_json().encode())
    )
    reader = _reader(stream)

    msg_type, body = await read_message(reader)
    assert msg_type == MSG_HELLO
    assert decode_json(SessionHello, body) == hello

    msg_type, body = await read_message(reader)
    assert msg_type == MSG_AUDIO
    assert decode_audio(body) == (7, b"pcm-bytes")

    msg_type, body = await read_message(reader)
    assert decode_json(Event, body).event == "started"

    assert await read_message(reader) is None  # clean EOF


async def test_torn_message_is_eof() -> None:
    whole = encode_audio(1, b"x" * 100)
    assert await read_message(_reader(whole[:20])) is None


async def test_decode_audio_rejects_short_body() -> None:
    with pytest.raises(ProtocolError):
        decode_audio(b"\x00")


async def test_decode_json_rejects_garbage() -> None:
    with pytest.raises(ProtocolError):
        decode_json(SessionHello, b"not json")
