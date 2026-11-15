"""The moment tier end to end: audio and a photo from another session become one
moment, described by the stub chat model, with links between them."""

import asyncio
import time
from datetime import timedelta

import httpx

from database.pipe import DatabasePipe
from database.schema.jobs import JobStatus
from tests.e2e.conftest import Account, ingest, make_session, wait_for_job
from tests.visual_media import jpeg_bytes
from tests.wav import wav_bytes


async def _moments(client: httpx.AsyncClient, account: Account, timeout: float = 45.0) -> list[dict]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = await client.get("/v1/moments", headers=account.headers)
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        # A moment can be built before the transcripts land; it is rebuilt when they do.
        if items and items[0]["counts"]["photos"] and items[0]["counts"]["utterances"] == 2:
            return items
        await asyncio.sleep(0.3)
    async with DatabasePipe() as pipe:
        jobs = await pipe.connection.execute(
            "SELECT status, error, result, payload FROM processing_jobs WHERE pipeline = 'moment'"
        )
        rows = await jobs.fetchall()
    raise AssertionError(f"no moment materialized: {items}; jobs: {rows}")


async def test_a_conversation_and_a_photo_become_one_linked_moment(
    worker: None, client: httpx.AsyncClient, account: Account, other_account: Account
) -> None:
    audio = await make_session(account)
    for _ in range(3):
        await ingest(audio.id, wav_bytes(seconds=0.1), duration_ms=100)
    async with DatabasePipe() as pipe:
        await pipe.sessions.end(audio.id)

    photos = await make_session(account, device="phone")
    upload = await client.post(
        f"/v1/sessions/{photos.id}/segments",
        files={"file": ("photo.jpg", jpeg_bytes(), "application/octet-stream")},
        data={
            "resource": "image",
            "captured_at": (audio.started_at + timedelta(milliseconds=100)).isoformat(),
        },
        headers=account.headers,
    )
    assert upload.status_code == 201, upload.text
    await wait_for_job(photos.id, "keyframe", JobStatus.SUCCEEDED, timeout=60)
    await wait_for_job(audio.id, "transcribe", JobStatus.SUCCEEDED, timeout=60)

    (moment,) = await _moments(client, account)
    assert set(moment["session_ids"]) == {str(audio.id), str(photos.id)}
    assert moment["title"] == "stub moment with 1 images"
    assert moment["model"] == "stub-llm"
    assert moment["counts"]["utterances"] == 2
    assert len(moment["cover"]) == 1

    detail = (await client.get(f"/v1/moments/{moment['id']}", headers=account.headers)).json()
    by_label = {item["label"]: item for item in detail["items"]}
    assert {"U1", "U2", "P1"} <= set(by_label)
    assert by_label["P1"]["kind"] == "photo"
    assert by_label["U1"]["text"] == "hello from the stub transcriber"
    photo = by_label["P1"]["artifact_id"]
    linked = {(l["from_artifact_id"], l["to_artifact_id"]) for l in detail["links"]}
    assert (photo, by_label["U1"]["artifact_id"]) in linked
    assert all(l["relation"] == "depicts" for l in detail["links"])  # the bogus X1 link is gone

    assert detail["previous_id"] is None and detail["next_id"] is None
    assert [s["name"] for s in moment["speakers"]] == ["voice 1", "voice 2"]

    found = await client.get(
        "/v1/moments/at", params={"session_id": str(audio.id), "at_ms": 50}, headers=account.headers
    )
    assert found.status_code == 200 and found.json()["id"] == moment["id"]
    missing = await client.get(
        "/v1/moments/at", params={"session_id": str(audio.id), "at_ms": 50}, headers=other_account.headers
    )
    assert missing.status_code == 404

    neighbours = (await client.get(f"/v1/moments/links/{photo}", headers=account.headers)).json()
    assert neighbours["moment_ids"] == [moment["id"]]
    assert len(neighbours["links"]) == len(detail["links"])

    assert (await client.get("/v1/moments", headers=other_account.headers)).json()["items"] == []
    theirs = await client.get(f"/v1/moments/{moment['id']}", headers=other_account.headers)
    assert theirs.status_code == 404
    assert (await client.get(f"/v1/moments/links/{photo}", headers=other_account.headers)).json()["links"] == []
