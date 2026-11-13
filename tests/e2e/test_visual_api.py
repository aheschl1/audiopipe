"""Image and video ingest: the REST upload, typed listings, media, timeline,
and the guarantee that audio pipelines never pick up a visual-only session."""

import json
from datetime import UTC, datetime, timedelta

import httpx

from database.pipe import DatabasePipe
from database.repos.pipelines.common import PipelineDiscovery
from database.schema.sessions import SessionCreate
from tests.e2e.conftest import Account, make_session

_JPEG = b"\xff\xd8\xff\xe0" + bytes(range(256)) * 4
_MP4 = b"\x00\x00\x00\x18ftypiso6" + bytes(range(256)) * 8


def _upload(client: httpx.AsyncClient, account: Account, session_id, payload, **form):
    content_type = form.pop("content_type", "application/octet-stream")
    return client.post(
        f"/v1/sessions/{session_id}/segments",
        files={"file": ("capture.bin", payload, content_type)},
        data=form,
        headers=account.headers,
    )


async def test_upload_image_and_video_then_list_and_fetch(
    client: httpx.AsyncClient, account: Account
) -> None:
    session = await make_session(account)
    at = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)

    image = await _upload(
        client, account, session.id, _JPEG,
        resource="image", captured_at=at.isoformat(),
        attrs=json.dumps({"width": 1280, "height": 720}),
        content_type="image/jpeg",
    )
    assert image.status_code == 201, image.text
    body = image.json()
    assert body["resource"] == "image"
    assert body["content_type"] == "image/jpeg"
    assert body["attrs"] == {"width": 1280, "height": 720}
    assert body["sequence"] == 0

    video = await _upload(
        client, account, session.id, _MP4,
        resource="video", captured_at=at.isoformat(), duration_ms="4000",
        metadata=json.dumps({"source": "rtmp"}),
    )
    assert video.status_code == 201, video.text
    assert video.json()["content_type"] == "video/mp4"
    assert video.json()["metadata"] == {"source": "rtmp"}
    assert video.json()["sequence"] == 1

    media = await client.get(
        f"/v1/segments/{body['id']}/media", headers=account.headers
    )
    assert media.status_code == 200
    assert media.content == _JPEG
    assert media.headers["content-type"] == "image/jpeg"

    listed = await client.get(
        f"/v1/sessions/{session.id}/segments", headers=account.headers
    )
    assert [item["resource"] for item in listed.json()["items"]] == ["image", "video"]


async def test_upload_rejections(client: httpx.AsyncClient, account: Account) -> None:
    session = await make_session(account)

    audio = await _upload(client, account, session.id, b"RIFF....", resource="audio")
    assert audio.status_code == 422

    garbage = await _upload(client, account, session.id, b"definitely not", resource="image")
    assert garbage.status_code == 422

    mismatched = await _upload(
        client, account, session.id, _JPEG, resource="image", content_type="image/png"
    )
    assert mismatched.status_code == 422

    no_duration = await _upload(client, account, session.id, _MP4, resource="video")
    assert no_duration.status_code == 422

    bad_attrs = await _upload(
        client, account, session.id, _JPEG, resource="image", attrs="[1, 2]"
    )
    assert bad_attrs.status_code == 422

    async with DatabasePipe() as pipe:
        await pipe.sessions.end(session.id)
    ended = await _upload(client, account, session.id, _JPEG, resource="image")
    assert ended.status_code == 409


async def test_visual_segments_on_timeline(
    client: httpx.AsyncClient, account: Account
) -> None:
    start = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)
    async with DatabasePipe() as pipe:
        session = await pipe.sessions.create(
            SessionCreate(user_id=account.user.id, started_at=start)
        )
    await _upload(
        client, account, session.id, _MP4,
        resource="video", captured_at=(start + timedelta(seconds=5)).isoformat(),
        duration_ms="4000",
    )
    await _upload(
        client, account, session.id, _JPEG,
        resource="image", captured_at=(start + timedelta(seconds=2)).isoformat(),
    )

    response = await client.get(
        f"/v1/sessions/{session.id}/timeline", headers=account.headers
    )
    events = [e for e in response.json()["items"] if e["type"] != "session-start"]
    assert [(e["type"], e["at_ms"]) for e in events] == [
        ("image", 2_000),
        ("video-clip", 5_000),
    ]
    assert events[1]["end_ms"] == 9_000
    assert events[0]["content_type"] == "image/jpeg"


async def test_audio_pipelines_skip_visual_only_sessions(
    client: httpx.AsyncClient, account: Account
) -> None:
    session = await make_session(account)
    await _upload(client, account, session.id, _JPEG, resource="image")
    async with DatabasePipe() as pipe:
        await pipe.sessions.end(session.id)
        found = await PipelineDiscovery(pipe.connection).ended_sessions_with_resource_without(
            "speech-detect", "audio", 1_000
        )
    assert session.id not in {s.id for s in found}
