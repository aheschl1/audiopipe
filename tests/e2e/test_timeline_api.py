"""GET /v1/sessions/{id}/timeline: ordering, paging, windows, and scoping."""

from datetime import datetime

import httpx

from database.pipe import DatabasePipe
from database.schema.artifacts import ArtifactCreate
from tests.e2e.conftest import Account, make_session

DEVICE_TIME = "2026-01-01T00:00:00+00:00"


async def _seed(session_id) -> dict[str, str]:
    """A speech map, two spans, and a transcript on the first span.

    The map is what diarize discovers, and the live worker would republish
    over the seeds mid-test (deleting the transcript — diarize's idiom clears
    transcribe artifacts too). A pre-succeeded diarize job on the map marks
    that session as already-processed, so the worker leaves it alone.
    """
    from database.schema.jobs import JobCreate

    async with DatabasePipe() as pipe:
        speech_map = await pipe.artifacts.create(
            ArtifactCreate(
                pipeline="speech-detect", kind="speech-map",
                session_id=session_id, metadata={"spans": 2},
            )
        )
        guard = await pipe.jobs.enqueue(
            JobCreate(
                pipeline="diarize", session_id=session_id,
                artifact_id=speech_map.id,
                dedup_key=f"diarize:map:{speech_map.id}",
            )
        )
        assert guard is not None
        # jobs.succeed only flips running jobs; this one must never run.
        await pipe.connection.execute(
            "UPDATE processing_jobs SET status = 'succeeded', finished_at = now() "
            "WHERE id = %s",
            (guard.id,),
        )
        span_a = await pipe.artifacts.create(
            ArtifactCreate(
                pipeline="speech-detect", kind="speech-span", session_id=session_id,
                start_ms=1_000, end_ms=4_000, metadata={"confidence": 0.9},
            )
        )
        span_b = await pipe.artifacts.create(
            ArtifactCreate(
                pipeline="speech-detect", kind="speech-span", session_id=session_id,
                start_ms=6_000, end_ms=9_000, metadata={"confidence": 0.8},
            )
        )
        transcript = await pipe.artifacts.create(
            ArtifactCreate(
                pipeline="transcribe", kind="transcript", session_id=session_id,
                start_ms=1_000, end_ms=4_000,
                links={"utterance": str(span_a.id)},
                metadata={
                    "text": "hello there", "chars": 11, "model": "stub",
                    "speaker": "b1000:SPEAKER_00",
                    "words": [
                        {"w": "hello", "s": 1_000, "e": 1_400},
                        {"w": "there", "s": 1_500, "e": 2_000},
                    ],
                },
            )
        )
    return {"span_a": str(span_a.id), "span_b": str(span_b.id), "transcript": str(transcript.id)}


async def test_timeline_orders_and_pages_events(
    client: httpx.AsyncClient, account: Account
) -> None:
    created = await client.post(
        "/v1/sessions",
        json={"device": "glasses-01", "started_at": DEVICE_TIME},
        headers=account.headers,
    )
    assert created.status_code == 201
    session_id = created.json()["id"]
    ids = await _seed(session_id)

    full = await client.get(f"/v1/sessions/{session_id}/timeline", headers=account.headers)
    assert full.status_code == 200
    items = full.json()["items"]
    assert [(item["type"], item["at_ms"]) for item in items] == [
        ("session-start", 0),
        ("speech-start", 1_000),
        ("transcript", 1_000),
        ("speech-end", 4_000),
        ("speech-start", 6_000),
        ("speech-end", 9_000),
    ]
    # The opening frame carries the device wall-clock time posted at creation.
    assert datetime.fromisoformat(items[0]["started_at"]) == datetime.fromisoformat(DEVICE_TIME)
    assert items[1]["artifact_id"] == ids["span_a"] and items[1]["confidence"] == 0.9
    assert items[2]["text"] == "hello there"
    assert items[2]["speaker"] == "b1000:SPEAKER_00"
    assert items[2]["end_ms"] == 4_000 and items[2]["artifact_id"] == ids["transcript"]

    # Offset paging over events: pages of two concatenate to the full list.
    paged: list = []
    offset, has_more = 0, True
    while has_more:
        page = await client.get(
            f"/v1/sessions/{session_id}/timeline",
            params={"limit": 2, "offset": offset},
            headers=account.headers,
        )
        body = page.json()
        paged += body["items"]
        has_more = body["has_more"]
        offset += 2
    assert paged == items

    # Ending the session appends the closing frame.
    ended = await client.post(f"/v1/sessions/{session_id}/end", headers=account.headers)
    after = await client.get(f"/v1/sessions/{session_id}/timeline", headers=account.headers)
    last = after.json()["items"][-1]
    assert last["type"] == "session-end"
    assert datetime.fromisoformat(last["ended_at"]) == datetime.fromisoformat(
        ended.json()["ended_at"]
    )


async def test_timeline_window_clips_by_event_position(
    client: httpx.AsyncClient, account: Account
) -> None:
    session = await make_session(account)
    await _seed(session.id)

    windowed = await client.get(
        f"/v1/sessions/{session.id}/timeline",
        params={"from_ms": 2_000, "to_ms": 9_000},
        headers=account.headers,
    )
    # The straddling span contributes only its end; 9000 is excluded (half-open).
    assert [(item["type"], item["at_ms"]) for item in windowed.json()["items"]] == [
        ("speech-end", 4_000),
        ("speech-start", 6_000),
    ]

    disjoint = await client.get(
        f"/v1/sessions/{session.id}/timeline",
        params={"from_ms": 20_000, "to_ms": 30_000},
        headers=account.headers,
    )
    assert disjoint.json()["items"] == []


async def test_timeline_is_scoped_to_the_owner(
    client: httpx.AsyncClient, account: Account, other_account: Account
) -> None:
    session = await make_session(account)
    await _seed(session.id)
    denied = await client.get(
        f"/v1/sessions/{session.id}/timeline", headers=other_account.headers
    )
    assert denied.status_code == 404


async def test_unprocessed_and_skipped_sessions_serve_only_session_frames(
    client: httpx.AsyncClient, account: Account
) -> None:
    session = await make_session(account)

    bare = await client.get(f"/v1/sessions/{session.id}/timeline", headers=account.headers)
    assert [item["type"] for item in bare.json()["items"]] == ["session-start"]

    async with DatabasePipe() as pipe:
        await pipe.artifacts.create(
            ArtifactCreate(
                pipeline="speech-detect", kind="speech-map", session_id=session.id,
                metadata={"spans": 0, "skipped": "no stitched audio"},
            )
        )
    skipped = await client.get(f"/v1/sessions/{session.id}/timeline", headers=account.headers)
    assert [item["type"] for item in skipped.json()["items"]] == ["session-start"]


async def test_edit_transcript_updates_timeline_and_search_source(
    client: httpx.AsyncClient, account: Account
) -> None:
    session = await make_session(account)
    ids = await _seed(session.id)

    edited = await client.post(
        f"/v1/sessions/{session.id}/transcripts/{ids['transcript']}",
        json={"text": "hello, there!"},
        headers=account.headers,
    )
    assert edited.status_code == 200
    metadata = edited.json()["metadata"]
    assert metadata["text"] == "hello, there!"
    assert metadata["chars"] == len("hello, there!")
    assert metadata["edited"] is True
    assert metadata["speaker"] == "b1000:SPEAKER_00"  # untouched keys survive
    assert "words" not in metadata  # edited text invalidates word timings

    timeline = await client.get(
        f"/v1/sessions/{session.id}/timeline", headers=account.headers
    )
    transcript = next(
        item for item in timeline.json()["items"] if item["type"] == "transcript"
    )
    assert transcript["text"] == "hello, there!"
    assert transcript["chars"] == len("hello, there!")

    # Empty text never silently erases an utterance.
    blank = await client.post(
        f"/v1/sessions/{session.id}/transcripts/{ids['transcript']}",
        json={"text": ""},
        headers=account.headers,
    )
    assert blank.status_code == 422


async def test_edit_transcript_rejects_wrong_artifacts_and_owners(
    client: httpx.AsyncClient, account: Account, other_account: Account
) -> None:
    session = await make_session(account)
    ids = await _seed(session.id)

    # A non-transcript artifact of the same session is not editable text.
    span = await client.post(
        f"/v1/sessions/{session.id}/transcripts/{ids['span_a']}",
        json={"text": "nope"},
        headers=account.headers,
    )
    assert span.status_code == 404

    # A transcript reached through someone else's session stays invisible.
    denied = await client.post(
        f"/v1/sessions/{session.id}/transcripts/{ids['transcript']}",
        json={"text": "nope"},
        headers=other_account.headers,
    )
    assert denied.status_code == 404

    # A transcript from another session does not answer to this session's path.
    unrelated = await make_session(account, device="glasses-02")
    mismatched = await client.post(
        f"/v1/sessions/{unrelated.id}/transcripts/{ids['transcript']}",
        json={"text": "nope"},
        headers=account.headers,
    )
    assert mismatched.status_code == 404


async def test_location_points_appear_live_and_interleaved(
    client: httpx.AsyncClient, account: Account
) -> None:
    """Raw location segments expand straight onto the timeline — visible on an
    OPEN session (no pipeline pass), interleaved with artifact events, and
    clipped by the same half-open windows."""
    from tests.e2e.conftest import ingest_location

    session = await make_session(account)
    await _seed(session.id)
    async with DatabasePipe() as pipe:
        loaded = await pipe.sessions.get(session.id)
    base = int(loaded.started_at.timestamp() * 1000)
    await ingest_location(
        session.id,
        [
            {"lat": 51.0, "lon": -114.0, "t": base + 500, "accuracy_m": 6.0},
            {"lat": 51.1, "lon": -114.1, "t": base + 5_000},
        ],
    )

    # The session is still open: points must be on the timeline already.
    listed = await client.get(
        f"/v1/sessions/{session.id}/timeline", headers=account.headers
    )
    events = listed.json()["items"]
    assert [event["type"] for event in events] == [
        "session-start",
        "location-point",   # 500
        "speech-start",     # 1000
        "transcript",       # 1000 (transcript ranks before location on ties)
        "speech-end",       # 4000
        "location-point",   # 5000
        "speech-start",     # 6000
        "speech-end",       # 9000
    ]
    first = events[1]
    assert (first["at_ms"], first["lat"], first["lon"]) == (500, 51.0, -114.0)
    assert first["accuracy_m"] == 6.0
    assert "segment_id" in first

    # Half-open windows partition the points like every other event.
    windowed = await client.get(
        f"/v1/sessions/{session.id}/timeline",
        params={"from_ms": 500, "to_ms": 5_000},
        headers=account.headers,
    )
    kinds = [e["type"] for e in windowed.json()["items"]]
    assert kinds.count("location-point") == 1
