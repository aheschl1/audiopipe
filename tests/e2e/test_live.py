"""The live layer end to end: frames cross the UDS, effect events come back.

The test env sets LIVE_WAKEWORD=wakemarker (the stub detector matches those
bytes inside PCM) and a 500 ms gate window, so a short stream sees the
counter's started and finished events while still connected.
"""

import json

import httpx

from api.schema.stream import encode_audio_frame
from tests.e2e.conftest import Account, make_session
from tests.e2e.test_stream import _connect, _drain_to_close, _recv_until
from tests.e2e.test_stream_v2 import FRAME_BYTES, _pcm_frames

FINISH = json.dumps({"type": "finish"})

HELLO_LIVE = json.dumps(
    {
        "type": "hello",
        "version": 2,
        "defaults": {"codec": "pcm_s16le", "sample_rate_hz": 16000, "channels": 1},
        "effects": ["live-counter", "no-such-effect"],
    }
)


def _wake_frame() -> bytes:
    marker = b"wakemarker"
    return b"\x00" * 100 + marker + b"\x00" * (FRAME_BYTES - 100 - len(marker))


async def test_wakeword_triggers_counter_events(
    server: str, client: httpx.AsyncClient, account: Account
) -> None:
    session = await make_session(account)
    frames = _pcm_frames(16)

    async with _connect(server, session.id, account) as ws:
        await ws.send(HELLO_LIVE)
        welcome = await _recv_until(ws, "welcome")
        assert welcome["effects"] == ["live-counter"]  # unknown names dropped

        for sequence in range(3):
            await ws.send(encode_audio_frame(sequence, frames[sequence]))
        await ws.send(encode_audio_frame(3, _wake_frame()))

        started = await _recv_until(ws, "effect")
        assert started["effect"] == "live-counter"
        assert started["event"] == "started"

        # 500ms window at 50ms frames: 10 frames close it and finish the run.
        for sequence in range(4, 16):
            await ws.send(encode_audio_frame(sequence, frames[sequence]))

        finished = await _recv_until(ws, "effect")
        assert finished["effect"] == "live-counter"
        assert finished["event"] == "finished"
        # The window's 10 frames plus up to 200ms (4 frames) of pre-roll.
        assert 10 <= finished["data"]["frames"] <= 14
        assert finished["data"]["bytes"] == finished["data"]["frames"] * FRAME_BYTES

        await ws.send(FINISH)
        events = await _drain_to_close(ws)

    assert events[-1]["type"] == "bye"
    assert events[-1]["through"] == 15

    # The durable path was untouched by the live tap: everything stored.
    listed = await client.get(f"/v1/sessions/{session.id}/segments", headers=account.headers)
    items = listed.json()["items"]
    assert items[-1]["metadata"]["frames"]["last"] == 15


async def test_no_effects_requested_still_streams(
    server: str, client: httpx.AsyncClient, account: Account
) -> None:
    """Frames are forwarded but no pipeline runs; no effect events appear."""
    session = await make_session(account)
    hello = json.dumps(
        {
            "type": "hello",
            "version": 2,
            "defaults": {"codec": "pcm_s16le", "sample_rate_hz": 16000, "channels": 1},
        }
    )
    async with _connect(server, session.id, account) as ws:
        await ws.send(hello)
        welcome = await _recv_until(ws, "welcome")
        assert welcome["effects"] == []
        await ws.send(encode_audio_frame(0, _wake_frame()))
        await ws.send(FINISH)
        events = await _drain_to_close(ws)

    assert not [event for event in events if event["type"] == "effect"]
    assert events[-1]["through"] == 0
