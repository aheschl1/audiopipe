"""Protocol v2 streaming: lean audio frames pooled into stored WAV segments.

The test env sets a 4 KiB pool target and a 0.3 s latency flush, so a handful
of 50 ms frames (1600 bytes each) spans several stored segments.
"""

import asyncio
import json

import httpx
import pytest
from websockets.exceptions import ConnectionClosed

from api.schema.stream import (
    FRAME_ENVELOPE,
    ChunkHeader,
    encode_audio_frame,
    encode_chunk,
)
from services.wav import WAV_HEADER_BYTES
from tests.e2e.conftest import Account, make_session
from tests.e2e.test_stream import _connect, _drain_to_close, _recv_event, _recv_until
from tests.wav import wav_bytes

HELLO_V2 = json.dumps(
    {
        "type": "hello",
        "version": 2,
        "defaults": {"codec": "pcm_s16le", "sample_rate_hz": 16000, "channels": 1},
    }
)
FINISH = json.dumps({"type": "finish"})

FRAME_BYTES = 1600  # 50 ms of 16 kHz mono s16le


def _pcm_frames(count: int) -> list[bytes]:
    """Slice one continuous sine tone into equal PCM frames."""
    pcm = wav_bytes(seconds=count * 0.05)[WAV_HEADER_BYTES:]
    return [pcm[i * FRAME_BYTES : (i + 1) * FRAME_BYTES] for i in range(count)]


async def test_v2_pools_frames_into_segments(
    server: str, client: httpx.AsyncClient, account: Account
) -> None:
    session = await make_session(account)
    frames = _pcm_frames(30)

    async with _connect(server, session.id, account) as ws:
        await ws.send(HELLO_V2)
        welcome = await _recv_event(ws)
        assert welcome["type"] == "welcome"
        assert welcome["version"] == 2
        assert welcome["next_sequence"] == 0
        assert welcome["limits"]["max_audio_frame_bytes"] == 65536

        for sequence, pcm in enumerate(frames):
            await ws.send(encode_audio_frame(sequence, pcm))
        await ws.send(FINISH)
        events = await _drain_to_close(ws)

    acks = [event for event in events if event["type"] == "ack"]
    assert acks[-1]["through"] == 29
    assert sum(ack["count"] for ack in acks) == 30
    assert sum(ack["bytes"] for ack in acks) == 30 * FRAME_BYTES
    assert events[-1] == {"type": "bye", "reason": "finished", "through": 29}

    listed = await client.get(f"/v1/sessions/{session.id}/segments", headers=account.headers)
    items = listed.json()["items"]
    assert 1 < len(items) < 30, "frames were not pooled"
    assert all(item["content_type"] == "audio/wav" for item in items)
    ranges = [item["metadata"]["frames"] for item in items]
    assert ranges[0]["first"] == 0
    assert ranges[-1]["last"] == 29
    for previous, current in zip(ranges, ranges[1:]):
        assert current["first"] == previous["last"] + 1
    assert [item["sequence"] for item in items] == [r["last"] for r in ranges]

    stitched = await client.get(
        f"/v1/sessions/{session.id}/resources/audio/media", headers=account.headers
    )
    assert stitched.status_code == 200
    assert stitched.content[WAV_HEADER_BYTES:] == b"".join(frames)


async def test_v2_resume_after_disconnect(
    server: str, client: httpx.AsyncClient, account: Account
) -> None:
    session = await make_session(account)
    frames = _pcm_frames(15)

    async with _connect(server, session.id, account) as ws:
        await ws.send(HELLO_V2)
        await _recv_until(ws, "welcome")
        for sequence in range(10):
            await ws.send(encode_audio_frame(sequence, frames[sequence]))
        # An abrupt close: the server flushes the pool; nothing is lost.

    # The flush lands after the server observes the disconnect; wait it out.
    for _ in range(50):
        listed = await client.get(
            f"/v1/sessions/{session.id}/segments", headers=account.headers
        )
        items = listed.json()["items"]
        if items and items[-1]["sequence"] == 9:
            break
        await asyncio.sleep(0.1)
    else:
        raise AssertionError(f"disconnect flush never landed: {items}")

    async with _connect(server, session.id, account) as ws:
        await ws.send(HELLO_V2)
        welcome = await _recv_until(ws, "welcome")
        assert welcome["next_sequence"] == 10
        for sequence in range(10, 15):
            await ws.send(encode_audio_frame(sequence, frames[sequence]))
        await ws.send(FINISH)
        events = await _drain_to_close(ws)

    assert events[-1]["through"] == 14
    stitched = await client.get(
        f"/v1/sessions/{session.id}/resources/audio/media", headers=account.headers
    )
    assert stitched.content[WAV_HEADER_BYTES:] == b"".join(frames)


async def test_v2_interleaves_envelope_chunks(
    server: str, client: httpx.AsyncClient, account: Account
) -> None:
    session = await make_session(account)
    frames = _pcm_frames(4)
    points = {"points": [{"lat": 51.0, "lon": -114.0, "t": 1755205000123}]}

    async with _connect(server, session.id, account) as ws:
        await ws.send(HELLO_V2)
        await _recv_until(ws, "welcome")
        await ws.send(encode_audio_frame(0, frames[0]))
        await ws.send(encode_audio_frame(1, frames[1]))
        location = encode_chunk(
            ChunkHeader(sequence=2, resource="location", content_type="application/json"),
            json.dumps(points).encode(),
        )
        await ws.send(bytes((FRAME_ENVELOPE,)) + location)
        await ws.send(encode_audio_frame(3, frames[2]))
        await ws.send(encode_audio_frame(4, frames[3]))
        await ws.send(FINISH)
        events = await _drain_to_close(ws)

    assert events[-1]["type"] == "bye"
    assert events[-1]["through"] == 4

    listed = await client.get(f"/v1/sessions/{session.id}/segments", headers=account.headers)
    items = listed.json()["items"]
    by_resource = {item["resource"] for item in items}
    assert by_resource == {"audio", "location"}
    location_rows = [item for item in items if item["resource"] == "location"]
    assert [item["sequence"] for item in location_rows] == [2]


async def test_v2_rejects_bad_frames_and_continues(
    server: str, client: httpx.AsyncClient, account: Account
) -> None:
    session = await make_session(account)
    frames = _pcm_frames(2)

    async with _connect(server, session.id, account) as ws:
        await ws.send(HELLO_V2)
        await _recv_until(ws, "welcome")

        await ws.send(b"\x7f" + b"junk")  # unknown discriminator
        error = await _recv_until(ws, "error")
        assert error["scope"] == "chunk"
        assert error["code"] == "bad_frame"

        await ws.send(encode_audio_frame(0, b"\x00"))  # misaligned for s16le
        error = await _recv_until(ws, "error")
        assert error["code"] == "bad_frame"
        assert error["sequence"] == 0

        await ws.send(encode_audio_frame(5, frames[0]))
        await ws.send(encode_audio_frame(3, frames[1]))  # regression, unacked
        error = await _recv_until(ws, "error")
        assert error["code"] == "bad_sequence"
        assert error["sequence"] == 3

        await ws.send(FINISH)
        events = await _drain_to_close(ws)

    assert events[-1]["type"] == "bye"
    # Only sequence 5 stored; through stays behind the gap at 0..4.
    listed = await client.get(f"/v1/sessions/{session.id}/segments", headers=account.headers)
    assert [item["sequence"] for item in listed.json()["items"]] == [5]


async def test_v2_hello_requires_pcm_parameters(
    server: str, account: Account
) -> None:
    session = await make_session(account)
    bad_hello = json.dumps(
        {"type": "hello", "version": 2, "defaults": {"content_type": "audio/wav"}}
    )
    async with _connect(server, session.id, account) as ws:
        await ws.send(bad_hello)
        with pytest.raises(ConnectionClosed):
            await ws.recv()
        assert ws.close_code == 4400


async def test_v2_latency_timer_flushes_quiet_pool(
    server: str, client: httpx.AsyncClient, account: Account
) -> None:
    session = await make_session(account)
    [pcm] = _pcm_frames(1)

    async with _connect(server, session.id, account) as ws:
        await ws.send(HELLO_V2)
        await _recv_until(ws, "welcome")
        await ws.send(encode_audio_frame(0, pcm))  # far below the 4 KiB target
        ack = await _recv_until(ws, "ack")  # latency flush at 0.3s, ack at 0.5s
        assert ack["through"] == 0
        await ws.send(FINISH)
        await _drain_to_close(ws)

    listed = await client.get(f"/v1/sessions/{session.id}/segments", headers=account.headers)
    [item] = listed.json()["items"]
    assert item["metadata"]["frames"] == {"first": 0, "last": 0, "count": 1}
