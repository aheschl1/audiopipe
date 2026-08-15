"""Segment metadata endpoints."""

import hashlib
from uuid import uuid4

import httpx

from tests.e2e.conftest import Account, ingest, make_session
from tests.wav import wav_bytes

INTERNAL_FIELDS = {"bucket", "object_key", "user_id"}


async def test_segment_detail_shape(client: httpx.AsyncClient, account: Account) -> None:
    session = await make_session(account)
    payload = wav_bytes(seconds=0.3)
    segment = await ingest(session.id, payload, duration_ms=300, offset_ms=0)

    body = (await client.get(f"/v1/segments/{segment.id}", headers=account.headers)).json()
    assert body["id"] == str(segment.id)
    assert body["session_id"] == str(session.id)
    assert body["sequence"] == 0
    assert body["byte_size"] == len(payload)
    assert body["content_type"] == "audio/wav"
    assert body["checksum_sha256"] == hashlib.sha256(payload).hexdigest()
    assert body["duration_ms"] == 300
    assert body["offset_ms"] == 0


async def test_storage_layout_never_leaves_the_service(
    client: httpx.AsyncClient, account: Account
) -> None:
    session = await make_session(account)
    segment = await ingest(session.id, wav_bytes())

    detail = (await client.get(f"/v1/segments/{segment.id}", headers=account.headers)).json()
    listed = (await client.get("/v1/segments", headers=account.headers)).json()["items"][0]

    assert INTERNAL_FIELDS.isdisjoint(detail)
    assert INTERNAL_FIELDS.isdisjoint(listed)


async def test_session_segments_are_in_capture_order(
    client: httpx.AsyncClient, account: Account
) -> None:
    session = await make_session(account)
    for freq in (440, 660, 880):
        await ingest(session.id, wav_bytes(freq=freq))

    body = (await client.get(f"/v1/sessions/{session.id}/segments", headers=account.headers)).json()
    assert [item["sequence"] for item in body["items"]] == [0, 1, 2]


async def test_user_segments_are_newest_ingested_first(
    client: httpx.AsyncClient, account: Account
) -> None:
    first = await make_session(account, label="first")
    second = await make_session(account, label="second")
    early = await ingest(first.id, wav_bytes())
    late = await ingest(second.id, wav_bytes(freq=660))

    body = (await client.get("/v1/segments", headers=account.headers)).json()
    assert [item["id"] for item in body["items"]] == [str(late.id), str(early.id)]


async def test_segments_span_every_session(client: httpx.AsyncClient, account: Account) -> None:
    one = await make_session(account)
    two = await make_session(account)
    await ingest(one.id, wav_bytes())
    await ingest(two.id, wav_bytes())

    body = (await client.get("/v1/segments", headers=account.headers)).json()
    assert {item["session_id"] for item in body["items"]} == {str(one.id), str(two.id)}


async def test_empty_listings_are_empty_pages(client: httpx.AsyncClient, account: Account) -> None:
    body = (await client.get("/v1/segments", headers=account.headers)).json()
    assert body == {"items": [], "limit": 50, "offset": 0, "has_more": False}


async def test_unknown_segment_is_404(client: httpx.AsyncClient, account: Account) -> None:
    response = await client.get(f"/v1/segments/{uuid4()}", headers=account.headers)
    assert response.status_code == 404


async def test_usage_totals_track_ingests(client: httpx.AsyncClient, account: Account) -> None:
    session = await make_session(account)
    payloads = [wav_bytes(seconds=0.2), wav_bytes(seconds=0.3)]
    for payload in payloads:
        await ingest(session.id, payload)

    usage = (await client.get("/v1/me", headers=account.headers)).json()["usage"]
    assert usage == [
        {"resource": "audio", "segments": 2, "total_bytes": sum(len(p) for p in payloads)}
    ]
