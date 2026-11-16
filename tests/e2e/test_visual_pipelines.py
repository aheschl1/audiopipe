"""The thumbnail and video-proxy pipelines end to end: uploaded segments are
processed, the timeline carries the derivatives, and they are served."""

from datetime import UTC, datetime, timedelta

import httpx

from database.schema.jobs import JobStatus
from tests.e2e.conftest import Account, make_session, wait_for_job
from tests.visual_media import jpeg_bytes, mp4_bytes


async def _wait_for_jobs(session_id, pipeline: str, count: int, timeout: float = 60.0) -> None:
    """Until ``count`` jobs of ``pipeline`` succeeded (wait_for_job returns on the first)."""
    import asyncio
    import time

    from database.pipe import DatabasePipe

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async with DatabasePipe() as pipe:
            jobs = await pipe.jobs.list_for_session(session_id)
        if sum(j.pipeline == pipeline and j.status == JobStatus.SUCCEEDED for j in jobs) >= count:
            return
        await asyncio.sleep(0.1)
    await wait_for_job(session_id, pipeline, JobStatus.SUCCEEDED, timeout=0)  # raises with the job list


async def _upload(client, account, session_id, payload, **form):
    response = await client.post(
        f"/v1/sessions/{session_id}/segments",
        files={"file": ("capture.bin", payload, "application/octet-stream")},
        data=form,
        headers=account.headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_derivatives_land_on_the_timeline_and_serve(
    worker: None, client: httpx.AsyncClient, account: Account, other_account: Account
) -> None:
    session = await make_session(account)
    start = session.started_at
    image = await _upload(
        client, account, session.id, jpeg_bytes(),
        resource="image", captured_at=(start + timedelta(seconds=1)).isoformat(),
    )
    video = await _upload(
        client, account, session.id, mp4_bytes(seconds=3),
        resource="video", captured_at=(start + timedelta(seconds=2)).isoformat(),
        duration_ms="3000",
    )
    await wait_for_job(session.id, "video-proxy", JobStatus.SUCCEEDED, timeout=60)
    await _wait_for_jobs(session.id, "thumbnail", 2)  # one per segment

    timeline = await client.get(f"/v1/sessions/{session.id}/timeline", headers=account.headers)
    events = {e["type"]: e for e in timeline.json()["items"]}
    assert events["image"]["segment_id"] == image["id"]
    assert events["image"]["thumbnail_id"]
    clip = events["video-clip"]
    assert clip["segment_id"] == video["id"]
    assert clip["proxy_id"]
    assert len(clip["frames"]) == 3
    assert [f["offset_ms"] for f in clip["frames"]] == sorted(f["offset_ms"] for f in clip["frames"])

    thumb = await client.get(
        f"/v1/artifacts/{events['image']['thumbnail_id']}/media", headers=account.headers
    )
    assert thumb.status_code == 200
    assert thumb.headers["content-type"] == "image/jpeg"

    proxy_url = f"/v1/artifacts/{clip['proxy_id']}/media"
    whole = await client.get(proxy_url, headers=account.headers)
    assert whole.headers["content-type"] == "video/mp4"
    assert whole.content.index(b"moov") < whole.content.index(b"mdat")
    ranged = await client.get(proxy_url, headers={**account.headers, "Range": "bytes=0-99"})
    assert ranged.status_code == 206 and len(ranged.content) == 100

    assert (await client.get(proxy_url)).status_code == 401
    assert (await client.get(proxy_url, headers=other_account.headers)).status_code == 404


async def test_artifacts_without_a_servable_blob_are_404(
    worker: None, client: httpx.AsyncClient, account: Account
) -> None:
    from database.pipe import DatabasePipe
    from database.schema.artifacts import ArtifactCreate

    session = await make_session(account)
    async with DatabasePipe() as pipe:
        artifact = await pipe.artifacts.create(
            ArtifactCreate(pipeline="session-stats", kind="session-stats", session_id=session.id)
        )
    response = await client.get(f"/v1/artifacts/{artifact.id}/media", headers=account.headers)
    assert response.status_code == 404


async def test_keyframes_are_placed_embedded_and_searchable(
    worker: None, client: httpx.AsyncClient, account: Account, other_account: Account
) -> None:
    session = await make_session(account)
    start = session.started_at
    image = await _upload(
        client, account, session.id, jpeg_bytes(),
        resource="image", captured_at=(start + timedelta(seconds=1)).isoformat(),
    )
    video = await _upload(
        client, account, session.id, mp4_bytes(seconds=3),
        resource="video", captured_at=(start + timedelta(seconds=5)).isoformat(),
        duration_ms="3000",
    )
    await _wait_for_jobs(session.id, "keyframe", 2)

    from database.pipe import DatabasePipe

    async with DatabasePipe() as pipe:
        frames = await pipe.artifacts.list_for_session(session.id, pipeline="keyframe")
    by_segment = {f.links["segment"]: f for f in frames}
    assert set(by_segment) == {image["id"], video["id"]}
    assert by_segment[image["id"]].start_ms == 1000
    assert by_segment[image["id"]].metadata["reason"] == "photo"
    # The stub embeds every frame identically: the clip is one scene.
    assert by_segment[video["id"]].metadata["reason"] == "first"
    assert 5000 <= by_segment[video["id"]].start_ms < 8000

    found = await client.get(
        "/v1/search/frames", params={"q": "a gradient"}, headers=account.headers
    )
    assert found.status_code == 200, found.text
    body = found.json()
    assert body["model"] == "stub-siglip"
    assert {hit["segment_id"] for hit in body["items"]} == {image["id"], video["id"]}
    assert all(abs(hit["distance"]) < 1e-6 for hit in body["items"])

    jpeg = await client.get(
        f"/v1/artifacts/{body['items'][0]['artifact_id']}/media", headers=account.headers
    )
    assert jpeg.headers["content-type"] == "image/jpeg"

    theirs = await client.get(
        "/v1/search/frames", params={"q": "a gradient"}, headers=other_account.headers
    )
    assert theirs.json()["items"] == []


async def test_a_rerun_replaces_its_earlier_output(account: Account) -> None:
    # A worker killed between writing artifacts and finishing the job runs it again.
    from uuid import uuid4

    from database.pipe import DatabasePipe
    from database.schema.jobs import Job
    from processing.pipelines.visual import ThumbnailPipeline
    from services import segments as segment_service

    session = await make_session(account)
    segment = await segment_service.ingest_segment(
        session.id, mp4_bytes(seconds=3), resource="video", upload=True,
        captured_at=session.started_at, duration_ms=3000,
    )
    job = Job(
        id=uuid4(), pipeline="thumbnail", session_id=session.id, priority=0,
        status=JobStatus.RUNNING, attempts=1, max_attempts=5,
        run_at=datetime.now(UTC), created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
        payload={"segment_id": str(segment.id)},
    )
    pipeline = ThumbnailPipeline()
    first = await pipeline.process(job)
    await pipeline.process(job)
    async with DatabasePipe() as pipe:
        stored = await pipe.artifacts.list_for_session(session.id, pipeline="thumbnail")
    assert len(stored) == first["artifacts"] == 3
