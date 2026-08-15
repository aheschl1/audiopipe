"""`/v1/me` and the session endpoints."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx

from database.pipe import DatabasePipe
from tests.e2e.conftest import Account, make_session


async def test_me_reports_identity_and_empty_usage(
    client: httpx.AsyncClient, account: Account
) -> None:
    body = (await client.get("/v1/me", headers=account.headers)).json()
    assert body["user"]["id"] == str(account.user.id)
    assert body["user"]["email"] == account.user.email
    assert body["usage"] == []


async def test_sessions_are_listed_newest_first(
    client: httpx.AsyncClient, account: Account
) -> None:
    now = datetime.now(UTC)
    from database.schema.sessions import SessionCreate

    async with DatabasePipe() as pipe:
        older = await pipe.sessions.create(
            SessionCreate(user_id=account.user.id, label="older", started_at=now - timedelta(1))
        )
        newer = await pipe.sessions.create(
            SessionCreate(user_id=account.user.id, label="newer", started_at=now)
        )

    body = (await client.get("/v1/sessions", headers=account.headers)).json()
    assert [item["id"] for item in body["items"]] == [str(newer.id), str(older.id)]
    assert body["has_more"] is False


async def test_open_only_filters_ended_sessions(
    client: httpx.AsyncClient, account: Account
) -> None:
    open_session = await make_session(account, label="open")
    ended = await make_session(account, label="ended")
    async with DatabasePipe() as pipe:
        await pipe.sessions.end(ended.id)

    body = (await client.get("/v1/sessions?open_only=true", headers=account.headers)).json()
    assert [item["id"] for item in body["items"]] == [str(open_session.id)]

    everything = (await client.get("/v1/sessions", headers=account.headers)).json()
    assert len(everything["items"]) == 2


async def test_session_detail_shape(client: httpx.AsyncClient, account: Account) -> None:
    created = await make_session(account, device="glasses-01", label="walk")

    body = (await client.get(f"/v1/sessions/{created.id}", headers=account.headers)).json()
    assert body["id"] == str(created.id)
    assert body["device"] == "glasses-01"
    assert body["label"] == "walk"
    assert body["is_open"] is True
    assert body["ended_at"] is None
    assert body["metadata"] == {}
    # The caller's own id is never echoed back.
    assert "user_id" not in body


async def test_ended_session_reports_closed(client: httpx.AsyncClient, account: Account) -> None:
    created = await make_session(account)
    async with DatabasePipe() as pipe:
        await pipe.sessions.end(created.id)

    body = (await client.get(f"/v1/sessions/{created.id}", headers=account.headers)).json()
    assert body["is_open"] is False
    assert body["ended_at"] is not None


async def test_label_renames_a_session(client: httpx.AsyncClient, account: Account) -> None:
    created = await make_session(account, device="glasses-01", label="walk")

    renamed = await client.post(
        f"/v1/sessions/{created.id}/label",
        headers=account.headers,
        json={"label": "morning walk"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["label"] == "morning walk"

    # The rename is what the session route serves from then on.
    body = (await client.get(f"/v1/sessions/{created.id}", headers=account.headers)).json()
    assert body["label"] == "morning walk"
    assert body["device"] == "glasses-01"

    # An explicit null clears it; nothing else about the session moves.
    cleared = await client.post(
        f"/v1/sessions/{created.id}/label", headers=account.headers, json={"label": None}
    )
    assert cleared.status_code == 200
    assert cleared.json()["label"] is None
    assert cleared.json()["device"] == "glasses-01"


async def test_label_requires_an_explicit_value(
    client: httpx.AsyncClient, account: Account
) -> None:
    """An empty body must not silently clear the name."""
    created = await make_session(account, label="walk")

    response = await client.post(
        f"/v1/sessions/{created.id}/label", headers=account.headers, json={}
    )
    assert response.status_code == 422

    body = (await client.get(f"/v1/sessions/{created.id}", headers=account.headers)).json()
    assert body["label"] == "walk"


async def test_ended_session_can_still_be_renamed(
    client: httpx.AsyncClient, account: Account
) -> None:
    created = await make_session(account)
    async with DatabasePipe() as pipe:
        await pipe.sessions.end(created.id)

    response = await client.post(
        f"/v1/sessions/{created.id}/label", headers=account.headers, json={"label": "after"}
    )
    assert response.status_code == 200
    assert response.json()["label"] == "after"
    assert response.json()["is_open"] is False


async def test_label_on_another_users_session_is_404(
    client: httpx.AsyncClient, account: Account, other_account: Account
) -> None:
    theirs = await make_session(other_account, label="not yours")

    response = await client.post(
        f"/v1/sessions/{theirs.id}/label", headers=account.headers, json={"label": "mine now"}
    )
    assert response.status_code == 404

    body = (await client.get(f"/v1/sessions/{theirs.id}", headers=other_account.headers)).json()
    assert body["label"] == "not yours"


async def test_unknown_session_is_404(client: httpx.AsyncClient, account: Account) -> None:
    response = await client.get(f"/v1/sessions/{uuid4()}", headers=account.headers)
    assert response.status_code == 404


async def test_non_uuid_path_is_422(client: httpx.AsyncClient, account: Account) -> None:
    response = await client.get("/v1/sessions/not-a-uuid", headers=account.headers)
    assert response.status_code == 422


# -- stitched session audio ---------------------------------------------------


def _expected_stitch(payloads: list[bytes]) -> bytes:
    """Build the stitched WAV the way the spec describes it, independently."""
    import struct

    data = b"".join(payload[44:] for payload in payloads)
    header = bytearray(payloads[0][:44])
    header[4:8] = struct.pack("<I", 36 + len(data))
    header[40:44] = struct.pack("<I", len(data))
    return bytes(header) + data


async def test_session_audio_stitches_every_segment(
    client: httpx.AsyncClient, account: Account
) -> None:
    from tests.e2e.conftest import ingest
    from tests.wav import wav_bytes

    session = await make_session(account)
    payloads = [wav_bytes(seconds=0.05, freq=300 + 50 * i) for i in range(3)]
    for payload in payloads:
        await ingest(session.id, payload)
    expected = _expected_stitch(payloads)

    response = await client.get(f"/v1/sessions/{session.id}/resources/audio/media", headers=account.headers)
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.headers["content-length"] == str(len(expected))
    assert response.content == expected

    # Conditional GET: the ETag covers the exact segment set.
    etag = response.headers["etag"]
    cached = await client.get(
        f"/v1/sessions/{session.id}/resources/audio/media",
        headers={**account.headers, "If-None-Match": etag},
    )
    assert cached.status_code == 304

    # A new chunk changes the audio, so the validator must change too.
    await ingest(session.id, wav_bytes(seconds=0.05, freq=999))
    grown = await client.get(
        f"/v1/sessions/{session.id}/resources/audio/media",
        headers={**account.headers, "If-None-Match": etag},
    )
    assert grown.status_code == 200
    assert grown.headers["etag"] != etag


async def test_session_audio_range_spans_segment_boundaries(
    client: httpx.AsyncClient, account: Account
) -> None:
    from tests.e2e.conftest import ingest
    from tests.wav import wav_bytes

    session = await make_session(account)
    payloads = [wav_bytes(seconds=0.05, freq=300 + 50 * i) for i in range(3)]
    for payload in payloads:
        await ingest(session.id, payload)
    expected = _expected_stitch(payloads)

    # A slice crossing from inside the first segment into the second.
    start, end = 40, len(payloads[0]) + 100
    response = await client.get(
        f"/v1/sessions/{session.id}/resources/audio/media",
        headers={**account.headers, "Range": f"bytes={start}-{end}"},
    )
    assert response.status_code == 206
    assert response.content == expected[start : end + 1]
    assert response.headers["content-range"] == f"bytes {start}-{end}/{len(expected)}"

    past_the_end = await client.get(
        f"/v1/sessions/{session.id}/resources/audio/media",
        headers={**account.headers, "Range": f"bytes={len(expected)}-"},
    )
    assert past_the_end.status_code == 416


async def test_session_audio_refuses_mixed_content_types(
    client: httpx.AsyncClient, account: Account
) -> None:
    from services import segments as segment_service
    from tests.e2e.conftest import ingest
    from tests.wav import wav_bytes

    session = await make_session(account)
    await ingest(session.id, wav_bytes(seconds=0.05))
    await segment_service.ingest_segment(session.id, b"not-wav-bytes", content_type="audio/webm")

    response = await client.get(f"/v1/sessions/{session.id}/resources/audio/media", headers=account.headers)
    assert response.status_code == 409


async def test_session_audio_404_when_empty(
    client: httpx.AsyncClient, account: Account
) -> None:
    session = await make_session(account)
    response = await client.get(f"/v1/sessions/{session.id}/resources/audio/media", headers=account.headers)
    assert response.status_code == 404
