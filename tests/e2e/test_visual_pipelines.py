"""The thumbnail and video-proxy pipelines end to end: uploaded segments are
processed, the timeline carries the derivatives, and they are served."""

from datetime import UTC, datetime, timedelta

import httpx

from database.schema.jobs import JobStatus
from tests.e2e.conftest import Account, make_session, wait_for_job
from tests.visual_media import jpeg_bytes, mp4_bytes


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
    for _ in range(2):  # one thumbnail job per segment
        await wait_for_job(session.id, "thumbnail", JobStatus.SUCCEEDED, timeout=60)

    timeline = await client.get(f"/v1/sessions/{session.id}/timeline", headers=account.headers)
    events = {e["type"]: e for e in timeline.json()["items"]}
    assert events["image"]["segment_id"] == image["id"]
    assert events["image"]["thumbnail_id"]
    clip = events["video-clip"]
    assert clip["segment_id"] == video["id"]
    assert clip["proxy_id"]
    assert len(clip["frames"]) == 3
    assert [f["offset_ms"] for f in clip["frames"]] == sorted(f["offset_ms"] for f in clip["frames"])

    token = (
        await client.post(f"/v1/sessions/{session.id}/playback", headers=account.headers)
    ).json()["token"]
    thumb = await client.get(
        f"/v1/artifacts/{events['image']['thumbnail_id']}/media", params={"token": token}
    )
    assert thumb.status_code == 200
    assert thumb.headers["content-type"] == "image/jpeg"

    proxy_url = f"/v1/artifacts/{clip['proxy_id']}/media"
    whole = await client.get(proxy_url, params={"token": token})
    assert whole.headers["content-type"] == "video/mp4"
    assert whole.content.index(b"moov") < whole.content.index(b"mdat")
    ranged = await client.get(proxy_url, params={"token": token}, headers={"Range": "bytes=0-99"})
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
