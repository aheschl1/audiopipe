"""The streaming upload websocket, driven end to end against the real server.

The test env (conftest) shrinks the ack window to 5 chunks / 0.5 s and the
chunk cap to 256 KiB, so windowed behaviour is observable without bulk data.
"""

import json
from typing import Any
from uuid import uuid4

import httpx
import pytest
import websockets
from websockets.exceptions import ConnectionClosed, ConnectionClosedError, InvalidStatus

from api.schema.stream import ChunkHeader, encode_chunk
from tests.e2e.conftest import Account, make_session
from tests.wav import wav_bytes

HELLO = json.dumps({"type": "hello", "version": 1, "defaults": {"content_type": "audio/wav"}})
FINISH = json.dumps({"type": "finish"})


def _url(server: str, session_id: Any) -> str:
    return f"{server.replace('http://', 'ws://')}/v1/sessions/{session_id}/stream"


def _connect(server: str, session_id: Any, account: Account | None):
    headers = account.headers if account else {}
    return websockets.connect(_url(server, session_id), additional_headers=headers)


def _chunk(sequence: int, payload: bytes, **fields: Any) -> bytes:
    return encode_chunk(ChunkHeader(sequence=sequence, **fields), payload)


async def _recv_event(ws: Any) -> dict[str, Any]:
    message = await ws.recv()
    assert isinstance(message, str), "server events are text frames"
    return json.loads(message)


async def _recv_until(ws: Any, event_type: str) -> dict[str, Any]:
    while True:
        event = await _recv_event(ws)
        if event["type"] == event_type:
            return event


async def _drain_to_close(ws: Any) -> list[dict[str, Any]]:
    events = []
    try:
        while True:
            events.append(await _recv_event(ws))
    except ConnectionClosed:
        return events


async def test_stream_happy_path(
    server: str, client: httpx.AsyncClient, account: Account
) -> None:
    created = await client.post(
        "/v1/sessions", json={"device": "glasses-01"}, headers=account.headers
    )
    assert created.status_code == 201
    session_id = created.json()["id"]

    payloads = [wav_bytes(seconds=0.05, freq=300 + 10 * i) for i in range(12)]
    async with _connect(server, session_id, account) as ws:
        await ws.send(HELLO)
        welcome = await _recv_event(ws)
        assert welcome["type"] == "welcome"
        assert welcome["version"] == 1
        assert welcome["next_sequence"] == 0
        assert welcome["ack_window"] == {"chunks": 5, "seconds": 0.5}

        for sequence, payload in enumerate(payloads):
            await ws.send(_chunk(sequence, payload, duration_ms=50))
        await ws.send(FINISH)
        events = await _drain_to_close(ws)

    acks = [event for event in events if event["type"] == "ack"]
    assert acks, f"no acks in {events}"
    # Windowed, not per-chunk: fewer acks than chunks, cumulative and complete.
    assert len(acks) < 12
    assert [ack["through"] for ack in acks] == sorted(ack["through"] for ack in acks)
    assert acks[-1]["through"] == 11
    assert sum(ack["count"] for ack in acks) == 12
    assert events[-1] == {"type": "bye", "reason": "finished", "through": 11}

    listed = await client.get(f"/v1/sessions/{session_id}/segments", headers=account.headers)
    items = listed.json()["items"]
    assert [item["sequence"] for item in items] == list(range(12))
    assert items[0]["content_type"] == "audio/wav"

    audio = await client.get(f"/v1/segments/{items[3]['id']}/media", headers=account.headers)
    assert audio.content == payloads[3]

    ended = await client.get(f"/v1/sessions/{session_id}", headers=account.headers)
    assert ended.json()["is_open"] is False


async def test_concurrent_stores_keep_acks_cumulative(
    server: str, client: httpx.AsyncClient, account: Account
) -> None:
    """A burst with disorder and a racing retransmit still acks contiguously.

    Chunk stores run concurrently server-side (API_STREAM_INGEST_CONCURRENCY),
    so completion order is arbitrary; `through` must still only advance over a
    gapless prefix, and the retransmit must surface as a duplicate, not a
    second row.
    """
    session = await make_session(account)
    payloads = [wav_bytes(seconds=0.05, freq=300 + 10 * i) for i in range(20)]

    async with _connect(server, session.id, account) as ws:
        await ws.send(HELLO)
        await _recv_until(ws, "welcome")
        # 3 goes out twice back-to-back (a retransmit racing its original) and
        # 5 lands before 4.
        for sequence in [0, 1, 2, 3, 3, 5, 4, *range(6, 20)]:
            await ws.send(_chunk(sequence, payloads[sequence]))
        await ws.send(FINISH)
        events = await _drain_to_close(ws)

    acks = [event for event in events if event["type"] == "ack"]
    assert [ack["through"] for ack in acks] == sorted(ack["through"] for ack in acks)
    assert acks[-1]["through"] == 19
    duplicates = [seq for ack in acks for seq in ack.get("duplicates", [])]
    assert duplicates == [3]
    assert sum(ack["count"] for ack in acks) == 20
    assert events[-1] == {"type": "bye", "reason": "finished", "through": 19}

    listed = await client.get(
        f"/v1/sessions/{session.id}/segments",
        params={"limit": 50},
        headers=account.headers,
    )
    assert [item["sequence"] for item in listed.json()["items"]] == list(range(20))


async def test_stream_resume_deduplicates_retransmits(
    server: str, client: httpx.AsyncClient, account: Account
) -> None:
    session = await make_session(account)
    payloads = [wav_bytes(seconds=0.05, freq=300 + 10 * i) for i in range(8)]

    async with _connect(server, session.id, account) as ws:
        await ws.send(HELLO)
        await _recv_until(ws, "welcome")
        for sequence, payload in enumerate(payloads):
            await ws.send(_chunk(sequence, payload))
        while (ack := await _recv_until(ws, "ack"))["through"] < 7:
            pass
        # Drop the connection without finish: the session must stay open.

    still_open = await client.get(f"/v1/sessions/{session.id}", headers=account.headers)
    assert still_open.json()["is_open"] is True

    async with _connect(server, session.id, account) as ws:
        await ws.send(HELLO)
        welcome = await _recv_until(ws, "welcome")
        assert welcome["next_sequence"] == 8
        # A client that never saw the last ack retransmits its tail.
        for sequence in (5, 6, 7):
            await ws.send(_chunk(sequence, payloads[sequence]))
        await ws.send(FINISH)
        events = await _drain_to_close(ws)

    duplicates = [seq for event in events if event["type"] == "ack" for seq in event["duplicates"]]
    assert duplicates == [5, 6, 7]
    assert events[-1]["type"] == "bye"
    assert events[-1]["through"] == 7

    listed = await client.get(f"/v1/sessions/{session.id}/segments", headers=account.headers)
    assert [item["sequence"] for item in listed.json()["items"]] == list(range(8))


async def test_duplicate_sequence_on_one_connection(server: str, account: Account) -> None:
    session = await make_session(account)
    payload = wav_bytes(seconds=0.05)

    async with _connect(server, session.id, account) as ws:
        await ws.send(HELLO)
        await _recv_until(ws, "welcome")
        await ws.send(_chunk(0, payload))
        await ws.send(_chunk(0, payload))
        ack = await _recv_until(ws, "ack")
        assert ack["through"] == 0
        assert ack["count"] == 1
        assert ack["duplicates"] == [0]


async def test_header_mode_handshake_rejections(
    server: str, client: httpx.AsyncClient, account: Account, other_account: Account
) -> None:
    """With an Authorization header present, failures reject the upgrade.

    A connection with no header at all is not rejected -- it enters hello-token
    mode, covered by test_hello_token_mode_rejections.
    """
    session = await make_session(account)

    async def status_for(session_id: Any, connecting: Account) -> int:
        with pytest.raises(InvalidStatus) as err:
            async with _connect(server, session_id, connecting):
                pass
        return err.value.response.status_code

    bogus = Account(user=account.user, token="not-a-token")
    assert await status_for(session.id, bogus) == 401
    assert await status_for(session.id, other_account) == 404
    assert await status_for(uuid4(), account) == 404

    ended = await client.post(f"/v1/sessions/{session.id}/end", headers=account.headers)
    assert ended.status_code == 200
    assert await status_for(session.id, account) == 409


async def test_first_frame_must_be_hello(server: str, account: Account) -> None:
    session = await make_session(account)
    async with _connect(server, session.id, account) as ws:
        await ws.send(FINISH)
        with pytest.raises(ConnectionClosedError) as err:
            await _recv_until(ws, "never")
        assert err.value.rcvd is not None
        assert err.value.rcvd.code == 4400


async def test_recoverable_chunk_errors_keep_streaming(
    server: str, client: httpx.AsyncClient, account: Account
) -> None:
    session = await make_session(account)
    good = wav_bytes(seconds=0.05)

    async with _connect(server, session.id, account) as ws:
        await ws.send(HELLO)
        await _recv_until(ws, "welcome")

        await ws.send(b"\x00")  # unparseable envelope
        error = await _recv_until(ws, "error")
        assert (error["scope"], error["code"]) == ("chunk", "bad_header")

        await ws.send(_chunk(0, b"\x00" * 300_000))  # over the 256 KiB test cap
        error = await _recv_until(ws, "error")
        assert (error["scope"], error["code"]) == ("chunk", "chunk_too_large")
        assert error["sequence"] == 0

        await ws.send(_chunk(0, good, checksum_sha256="00" * 32))
        error = await _recv_until(ws, "error")
        assert (error["scope"], error["code"]) == ("chunk", "checksum_mismatch")

        # The connection survived all three; the sequence is still available.
        await ws.send(_chunk(0, good))
        ack = await _recv_until(ws, "ack")
        assert ack["through"] == 0

    listed = await client.get(f"/v1/sessions/{session.id}/segments", headers=account.headers)
    assert len(listed.json()["items"]) == 1


async def test_session_ended_mid_stream_closes_4409(
    server: str, client: httpx.AsyncClient, account: Account
) -> None:
    session = await make_session(account)
    async with _connect(server, session.id, account) as ws:
        await ws.send(HELLO)
        await _recv_until(ws, "welcome")
        await ws.send(_chunk(0, wav_bytes(seconds=0.05)))
        await _recv_until(ws, "ack")

        ended = await client.post(f"/v1/sessions/{session.id}/end", headers=account.headers)
        assert ended.status_code == 200

        # Inside the raises block: the periodic session check may close the
        # socket before the chunk goes out, and that path is equally valid.
        with pytest.raises(ConnectionClosedError) as err:
            await ws.send(_chunk(1, wav_bytes(seconds=0.05)))
            while True:
                event = await _recv_event(ws)
                if event["type"] == "error":
                    assert (event["scope"], event["code"]) == ("session", "session_ended")
        assert err.value.rcvd is not None
        assert err.value.rcvd.code == 4409


async def test_split_pushes_rotate_to_a_quiet_stream(
    server: str, client: httpx.AsyncClient, account: Account
) -> None:
    """A split reaches a connected-but-silent client through the periodic
    session check: `rotate`, then `session_ended`, then close 4409."""
    session = await make_session(account)
    async with _connect(server, session.id, account) as ws:
        await ws.send(HELLO)
        await _recv_until(ws, "welcome")

        split = await client.post(f"/v1/sessions/{session.id}/split", headers=account.headers)
        assert split.status_code == 200
        assert split.json()["metadata"]["rotated"] is True

        events: list[dict[str, Any]] = []
        with pytest.raises(ConnectionClosedError) as err:
            while True:
                events.append(await _recv_event(ws))
        assert err.value.rcvd is not None
        assert err.value.rcvd.code == 4409

    assert [event["type"] for event in events] == ["rotate", "error"]
    assert events[0]["through"] == -1  # nothing was stored
    assert (events[1]["scope"], events[1]["code"]) == ("session", "session_ended")


async def test_plain_end_reaches_a_quiet_stream_without_rotate(
    server: str, client: httpx.AsyncClient, account: Account
) -> None:
    """An explicit end must not tell the device to re-record: no `rotate`."""
    session = await make_session(account)
    async with _connect(server, session.id, account) as ws:
        await ws.send(HELLO)
        await _recv_until(ws, "welcome")

        ended = await client.post(f"/v1/sessions/{session.id}/end", headers=account.headers)
        assert ended.status_code == 200

        events: list[dict[str, Any]] = []
        with pytest.raises(ConnectionClosedError) as err:
            while True:
                events.append(await _recv_event(ws))
        assert err.value.rcvd is not None
        assert err.value.rcvd.code == 4409

    assert [event["type"] for event in events] == ["error"]
    assert events[0]["code"] == "session_ended"


def _hello_with_token(token: str) -> str:
    return json.dumps(
        {"type": "hello", "version": 1, "token": token, "defaults": {"content_type": "audio/wav"}}
    )


async def test_hello_token_mode_happy_path(
    server: str, client: httpx.AsyncClient, account: Account
) -> None:
    """A header-less client (browser/embedded) authenticates inside hello."""
    session = await make_session(account)
    payloads = [wav_bytes(seconds=0.05, freq=300 + 10 * i) for i in range(3)]

    async with websockets.connect(_url(server, session.id)) as ws:  # no auth header
        await ws.send(_hello_with_token(account.token))
        welcome = await _recv_until(ws, "welcome")
        assert welcome["next_sequence"] == 0
        for sequence, payload in enumerate(payloads):
            await ws.send(_chunk(sequence, payload))
        await ws.send(FINISH)
        events = await _drain_to_close(ws)

    assert events[-1]["type"] == "bye"
    assert events[-1]["through"] == 2

    listed = await client.get(f"/v1/sessions/{session.id}/segments", headers=account.headers)
    assert [item["sequence"] for item in listed.json()["items"]] == [0, 1, 2]


async def test_hello_token_mode_rejections(
    server: str, client: httpx.AsyncClient, account: Account, other_account: Account
) -> None:
    """Handshake failures become close codes when auth rides in hello."""

    async def close_code_for(session_id: Any, token: str | None) -> int:
        async with websockets.connect(_url(server, session_id)) as ws:
            hello: dict[str, Any] = {"type": "hello", "version": 1}
            if token is not None:
                hello["token"] = token
            await ws.send(json.dumps(hello))
            with pytest.raises(ConnectionClosed) as err:
                await ws.recv()
            assert err.value.rcvd is not None
            return err.value.rcvd.code

    session = await make_session(account)
    assert await close_code_for(session.id, "not-a-token") == 1008
    assert await close_code_for(session.id, None) == 1008
    assert await close_code_for(session.id, other_account.token) == 4404
    assert await close_code_for(uuid4(), account.token) == 4404

    ended = await client.post(f"/v1/sessions/{session.id}/end", headers=account.headers)
    assert ended.status_code == 200
    assert await close_code_for(session.id, account.token) == 4409


async def test_stale_sessions_are_swept(
    server: str, client: httpx.AsyncClient, account: Account
) -> None:
    """The sweeper's repo primitive plus the 409 a late reconnect sees.

    The sweep itself is exercised directly rather than by shrinking the
    server's sweep interval, which would destabilise every other test.
    """
    from database.pipe import DatabasePipe

    session = await make_session(account)
    async with DatabasePipe() as pipe:
        # The updated_at trigger would defeat backdating, so bypass it.
        await pipe.connection.execute(
            "ALTER TABLE recording_sessions DISABLE TRIGGER recording_sessions_set_updated_at"
        )
        await pipe.connection.execute(
            "UPDATE recording_sessions SET updated_at = now() - interval '1 hour' WHERE id = %s",
            (session.id,),
        )
        await pipe.connection.execute(
            "ALTER TABLE recording_sessions ENABLE TRIGGER recording_sessions_set_updated_at"
        )

    async with DatabasePipe() as pipe:
        assert await pipe.sessions.end_stale(300) == 1
        assert await pipe.sessions.end_stale(300) == 0  # idempotent

    swept = await client.get(f"/v1/sessions/{session.id}", headers=account.headers)
    assert swept.json()["is_open"] is False

    with pytest.raises(InvalidStatus) as err:
        async with _connect(server, session.id, account):
            pass
    assert err.value.response.status_code == 409


def _location_chunk(sequence: int, points: list[dict[str, Any]], **fields: Any) -> bytes:
    return encode_chunk(
        ChunkHeader(sequence=sequence, resource="location", **fields),
        json.dumps({"points": points}).encode(),
    )


async def test_stream_interleaves_location_with_audio(
    server: str, client: httpx.AsyncClient, account: Account
) -> None:
    """Location batches ride the same socket, sequence space and acks.

    The audio chunks must stitch exactly as they would alone — interleaved
    location rows share the sequence counter but not the byte stream.
    """
    from database.pipe import DatabasePipe

    session = await make_session(account)
    payloads = [wav_bytes(seconds=0.05, freq=300 + 10 * i) for i in range(4)]
    batches = [
        [{"lat": 51.0, "lon": -114.0, "t": 1_000 + i, "accuracy_m": 5.0}] for i in range(2)
    ]

    async with _connect(server, session.id, account) as ws:
        await ws.send(HELLO)
        welcome = await _recv_until(ws, "welcome")
        assert "location" in welcome["resources"]

        await ws.send(_chunk(0, payloads[0]))
        await ws.send(_location_chunk(1, batches[0]))
        await ws.send(_chunk(2, payloads[1]))
        await ws.send(_chunk(3, payloads[2]))
        await ws.send(_location_chunk(4, batches[1]))
        await ws.send(_chunk(5, payloads[3]))
        await ws.send(FINISH)
        events = await _drain_to_close(ws)

    acks = [event for event in events if event["type"] == "ack"]
    assert acks[-1]["through"] == 5
    assert not [event for event in events if event["type"] == "error"]

    async with DatabasePipe() as pipe:
        rows = await pipe.segments.list_for_session(session.id)
        location_rows = await pipe.segments.list_for_session(session.id, resource="location")
    assert [row.resource for row in rows] == [
        "audio", "location", "audio", "audio", "location", "audio",
    ]
    assert [row.sequence for row in location_rows] == [1, 4]
    for row, batch in zip(location_rows, batches):
        assert row.payload == {"points": batch}
        assert row.bucket is None and row.object_key is None
        assert row.content_type == "application/json"
        assert row.captured_at is not None  # derived from the first point

    # The stitched audio is untouched by the interleaved rows.
    stitched = await client.get(f"/v1/sessions/{session.id}/resources/audio/media", headers=account.headers)
    assert stitched.status_code == 200
    body = stitched.content
    assert body[:4] == b"RIFF"
    assert len(body) == 44 + sum(len(p) - 44 for p in payloads)


async def test_stream_rejects_bad_location_batch_and_unknown_resource(
    server: str, client: httpx.AsyncClient, account: Account
) -> None:
    """A bad batch is a recoverable chunk error; the sequence retransmits."""
    session = await make_session(account)

    async with _connect(server, session.id, account) as ws:
        await ws.send(HELLO)
        await _recv_until(ws, "welcome")

        await ws.send(_location_chunk(0, [{"lat": 99.0, "lon": 0.0, "t": 1}]))
        error = await _recv_until(ws, "error")
        assert (error["code"], error["scope"]) == ("invalid_payload", "chunk")
        assert error["sequence"] == 0

        head = json.dumps({"sequence": 1, "resource": "video"}).encode()
        await ws.send(len(head).to_bytes(4, "big") + head + b"frame")
        error = await _recv_until(ws, "error")
        assert (error["code"], error["scope"]) == ("bad_header", "chunk")

        # Both sequences retransmit fine — nothing was stored.
        await ws.send(_location_chunk(0, [{"lat": 51.0, "lon": -114.0, "t": 1}]))
        await ws.send(_chunk(1, wav_bytes(seconds=0.05)))
        await ws.send(FINISH)
        events = await _drain_to_close(ws)

    acks = [event for event in events if event["type"] == "ack"]
    assert acks[-1]["through"] == 1
