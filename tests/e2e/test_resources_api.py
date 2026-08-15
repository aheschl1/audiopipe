"""The resource-oriented HTTP surface: session summaries, typed segment
listings, and the per-segment media route across storage modes."""

import json

import httpx

from tests.e2e.conftest import Account, ingest, ingest_location, make_session
from tests.wav import wav_bytes


async def test_session_resource_summary(
    client: httpx.AsyncClient, account: Account
) -> None:
    session = await make_session(account)
    payloads = [wav_bytes(seconds=0.1), wav_bytes(seconds=0.2)]
    for payload in payloads:
        await ingest(session.id, payload)
    await ingest_location(session.id, [{"lat": 51.0, "lon": -114.0, "t": 5_000}])

    response = await client.get(
        f"/v1/sessions/{session.id}/resources", headers=account.headers
    )
    assert response.status_code == 200
    rows = response.json()
    assert [row["resource"] for row in rows] == ["audio", "location"]
    audio_row, location_row = rows
    assert audio_row["segments"] == 2
    assert audio_row["total_bytes"] == sum(len(p) for p in payloads)
    assert location_row["segments"] == 1
    assert location_row["first_captured_at"] is not None


async def test_segment_listings_filter_and_discriminate(
    client: httpx.AsyncClient, account: Account
) -> None:
    session = await make_session(account)
    await ingest(session.id, wav_bytes(seconds=0.1))
    await ingest_location(
        session.id,
        [{"lat": 51.0, "lon": -114.0, "t": 1_000}, {"lat": 51.1, "lon": -114.1, "t": 2_000}],
    )

    listed = await client.get(
        f"/v1/sessions/{session.id}/segments", headers=account.headers
    )
    items = listed.json()["items"]
    assert [item["resource"] for item in items] == ["audio", "location"]
    assert items[0]["attrs"] == {"codec": None, "sample_rate_hz": None, "channels": None}
    assert items[1]["attrs"] == {"points": 2}

    only_location = await client.get(
        f"/v1/sessions/{session.id}/segments",
        params={"resource": "location"},
        headers=account.headers,
    )
    assert [item["resource"] for item in only_location.json()["items"]] == ["location"]

    across = await client.get(
        "/v1/segments", params={"resource": "location"}, headers=account.headers
    )
    assert [item["resource"] for item in across.json()["items"]] == ["location"]


async def test_media_serves_blob_and_inline(
    client: httpx.AsyncClient, account: Account
) -> None:
    session = await make_session(account)
    payload = wav_bytes(seconds=0.1)
    audio_segment = await ingest(session.id, payload)
    points = [{"lat": 51.0, "lon": -114.0, "t": 1_000}]
    location_segment = await ingest_location(session.id, points)

    blob = await client.get(
        f"/v1/segments/{audio_segment.id}/media", headers=account.headers
    )
    assert blob.status_code == 200
    assert blob.content == payload

    ranged = await client.get(
        f"/v1/segments/{audio_segment.id}/media",
        headers={**account.headers, "Range": "bytes=0-3"},
    )
    assert ranged.status_code == 206
    assert ranged.content == payload[:4]

    inline = await client.get(
        f"/v1/segments/{location_segment.id}/media", headers=account.headers
    )
    assert inline.status_code == 200
    assert inline.headers["content-type"].startswith("application/json")
    assert json.loads(inline.content) == {"points": points}

    revalidated = await client.get(
        f"/v1/segments/{location_segment.id}/media",
        headers={**account.headers, "If-None-Match": inline.headers["etag"]},
    )
    assert revalidated.status_code == 304


async def test_session_media_gates_on_renderability(
    client: httpx.AsyncClient, account: Account
) -> None:
    session = await make_session(account)
    payload = wav_bytes(seconds=0.1)
    await ingest(session.id, payload)

    rendered = await client.get(
        f"/v1/sessions/{session.id}/resources/audio/media", headers=account.headers
    )
    assert rendered.status_code == 200
    assert rendered.content[:4] == b"RIFF"

    non_renderable = await client.get(
        f"/v1/sessions/{session.id}/resources/location/media", headers=account.headers
    )
    assert non_renderable.status_code == 404
    assert "no rendered media" in non_renderable.json()["detail"]

    unknown = await client.get(
        f"/v1/sessions/{session.id}/resources/video/media", headers=account.headers
    )
    assert unknown.status_code == 404
    assert "unknown resource" in unknown.json()["detail"]
