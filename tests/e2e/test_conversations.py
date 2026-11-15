"""The conversation tier end to end: utterances grouped by gap, the list and
detail routes, the timeline event, and in-place membership curation."""

import httpx

from database.pipe import DatabasePipe
from database.schema.artifacts import ArtifactCreate
from database.schema.jobs import JobStatus
from tests.e2e.conftest import Account, make_session, wait_for_job

GAP_MS = 60_000  # PROCESSING_CONVERSATION_GAP_MS default


async def _seed(account: Account, utterances: list[tuple[int, int, str]]):
    """A session carrying hand-built utterances (+ transcripts) and a diarize-map.

    Left unended: an ended session would be picked up by the real speech
    tiers and republished over.
    """
    session = await make_session(account)
    async with DatabasePipe() as pipe:
        rows = await pipe.artifacts.create_many(
            [
                ArtifactCreate(
                    pipeline="diarize",
                    kind="utterance",
                    session_id=session.id,
                    start_ms=start,
                    end_ms=end,
                    metadata={"speaker": speaker, "block_start_ms": 0, "turns": 1},
                )
                for start, end, speaker in utterances
            ]
        )
        await pipe.artifacts.create_many(
            [
                ArtifactCreate(
                    pipeline="transcribe",
                    kind="transcript",
                    session_id=session.id,
                    start_ms=row.start_ms,
                    end_ms=row.end_ms,
                    links={"utterance": str(row.id)},
                    metadata={
                        "text": f"utterance at {row.start_ms}",
                        "speaker": row.metadata["speaker"],
                    },
                )
                for row in rows
            ]
        )
        # The map is the completion marker: writing it last mints the job.
        await pipe.artifacts.create(
            ArtifactCreate(
                pipeline="diarize",
                kind="diarize-map",
                session_id=session.id,
                metadata={"utterances": len(rows)},
            )
        )
    return session, rows


async def test_utterances_group_by_gap_and_lone_remarks_drop(
    worker: None, client: httpx.AsyncClient, account: Account
) -> None:
    session, rows = await _seed(
        account,
        [
            (0, 2_000, "b0:SPEAKER_00"),
            (3_000, 5_000, "b0:SPEAKER_01"),
            (6_000, 8_000, "b0:SPEAKER_00"),
            (8_000 + GAP_MS + 1, 10_000 + GAP_MS, "b0:SPEAKER_00"),  # lone remark
            (200_000, 202_000, "b0:SPEAKER_00"),
            (203_000, 205_000, "b0:SPEAKER_00"),
        ],
    )
    job = await wait_for_job(session.id, "conversation", JobStatus.SUCCEEDED)
    assert job.result == {"conversations": 2, "utterances": 6, "grouped": 5}

    response = await client.get(
        f"/v1/sessions/{session.id}/conversations", headers=account.headers
    )
    assert response.status_code == 200
    first, second = response.json()["items"]
    assert (first["start_ms"], first["end_ms"], first["turns"]) == (0, 8_000, 3)
    assert first["alternations"] == 2
    assert [s["label"] for s in first["speakers"]] == ["b0:SPEAKER_00", "b0:SPEAKER_01"]
    assert first["utterance_ids"] == [str(r.id) for r in rows[:3]]
    assert (first["opening"], first["closure"]) == ("session_start", "gap")
    async with DatabasePipe() as pipe:
        artifacts = await pipe.artifacts.list_for_session(session.id, kind="conversation")
    assert all(len(a.metadata["user_labels"]) == 1 for a in artifacts)
    assert first["gap_after_ms"] == GAP_MS + 1
    assert (second["turns"], second["alternations"]) == (2, 0)
    assert second["closure"] == "session_end"

    async with DatabasePipe() as pipe:
        (conversation_map,) = await pipe.artifacts.list_for_session(
            session.id, kind="conversation-map"
        )
    assert conversation_map.metadata["conversations"] == 2
    assert conversation_map.metadata["params"]["gap_ms"] == GAP_MS


async def test_detail_carries_the_ordered_transcript(
    worker: None, client: httpx.AsyncClient, account: Account
) -> None:
    session, rows = await _seed(
        account, [(0, 2_000, "b0:SPEAKER_00"), (3_000, 5_000, "b0:SPEAKER_01")]
    )
    await wait_for_job(session.id, "conversation", JobStatus.SUCCEEDED)
    listing = await client.get(
        f"/v1/sessions/{session.id}/conversations", headers=account.headers
    )
    (conversation,) = listing.json()["items"]

    response = await client.get(
        f"/v1/conversations/{conversation['id']}", headers=account.headers
    )
    assert response.status_code == 200
    detail = response.json()
    assert [t["utterance_id"] for t in detail["transcripts"]] == [str(r.id) for r in rows]
    assert [t["text"] for t in detail["transcripts"]] == [
        "utterance at 0",
        "utterance at 3000",
    ]
    assert detail["transcripts"][1]["speaker"] == "b0:SPEAKER_01"

    timeline = await client.get(
        f"/v1/sessions/{session.id}/timeline", headers=account.headers
    )
    types = [e["type"] for e in timeline.json()["items"]]
    assert types.index("conversation") < types.index("transcript")


async def test_other_users_cannot_see_it(
    worker: None, client: httpx.AsyncClient, account: Account, other_account: Account
) -> None:
    session, _ = await _seed(
        account, [(0, 2_000, "b0:SPEAKER_00"), (3_000, 5_000, "b0:SPEAKER_01")]
    )
    await wait_for_job(session.id, "conversation", JobStatus.SUCCEEDED)
    (conversation,) = (
        await client.get(
            f"/v1/sessions/{session.id}/conversations", headers=account.headers
        )
    ).json()["items"]
    response = await client.get(
        f"/v1/conversations/{conversation['id']}", headers=other_account.headers
    )
    assert response.status_code == 404


async def test_exclude_shrinks_and_include_restores(
    worker: None, client: httpx.AsyncClient, account: Account
) -> None:
    session, rows = await _seed(
        account,
        [
            (0, 2_000, "b0:SPEAKER_00"),
            (3_000, 5_000, "b0:SPEAKER_01"),
            (6_000, 8_000, "b0:SPEAKER_00"),
        ],
    )
    await wait_for_job(session.id, "conversation", JobStatus.SUCCEEDED)
    (conversation,) = (
        await client.get(
            f"/v1/sessions/{session.id}/conversations", headers=account.headers
        )
    ).json()["items"]
    url = f"/v1/conversations/{conversation['id']}"
    tail = rows[2]

    response = await client.post(
        f"{url}/exclude",
        headers=account.headers,
        json={"utterance_id": str(tail.id), "reason": "tv"},
    )
    assert response.status_code == 200
    shrunk = response.json()
    assert (shrunk["start_ms"], shrunk["end_ms"], shrunk["turns"]) == (0, 5_000, 2)
    assert shrunk["alternations"] == 1
    assert shrunk["utterance_ids"] == [str(r.id) for r in rows[:2]]
    assert shrunk["excluded"] == [
        {"utterance_id": str(tail.id), "reason": "tv", "source": "manual"}
    ]
    # The detail view follows the membership, not the span.
    detail = (await client.get(url, headers=account.headers)).json()
    assert len(detail["transcripts"]) == 2

    # Not a member any more: excluding it twice is a 404, not a duplicate.
    again = await client.post(
        f"{url}/exclude", headers=account.headers, json={"utterance_id": str(tail.id)}
    )
    assert again.status_code == 404

    response = await client.post(
        f"{url}/include", headers=account.headers, json={"utterance_id": str(tail.id)}
    )
    assert response.status_code == 200
    restored = response.json()
    assert (restored["end_ms"], restored["turns"], restored["alternations"]) == (8_000, 3, 2)
    assert restored["excluded"] == []
    assert restored["utterance_ids"] == [str(r.id) for r in rows]


async def test_excluding_below_min_turns_deletes_the_conversation(
    worker: None, client: httpx.AsyncClient, account: Account
) -> None:
    session, rows = await _seed(
        account, [(0, 2_000, "b0:SPEAKER_00"), (3_000, 5_000, "b0:SPEAKER_01")]
    )
    await wait_for_job(session.id, "conversation", JobStatus.SUCCEEDED)
    (conversation,) = (
        await client.get(
            f"/v1/sessions/{session.id}/conversations", headers=account.headers
        )
    ).json()["items"]
    url = f"/v1/conversations/{conversation['id']}"

    response = await client.post(
        f"{url}/exclude", headers=account.headers, json={"utterance_id": str(rows[0].id)}
    )
    assert response.status_code == 204
    assert (await client.get(url, headers=account.headers)).status_code == 404
    listing = await client.get(
        f"/v1/sessions/{session.id}/conversations", headers=account.headers
    )
    assert listing.json()["items"] == []


async def test_named_speakers_resolve_in_list_and_detail(
    worker: None, client: httpx.AsyncClient, account: Account
) -> None:
    """Through the real diarize → cluster path: a label put on a speaker
    cluster shows up on the conversation and on each of its transcripts."""
    from tests.e2e.test_speakers import _diarized_session

    session = await _diarized_session(account)
    await wait_for_job(session.id, "conversation", JobStatus.SUCCEEDED)
    speakers = (await client.get("/v1/speakers", headers=account.headers)).json()["items"]
    target = speakers[0]
    await client.post(
        f"/v1/speakers/{target['id']}/label", headers=account.headers, json={"name": "Mom"}
    )

    (conversation,) = (
        await client.get(
            f"/v1/sessions/{session.id}/conversations", headers=account.headers
        )
    ).json()["items"]
    by_id = {s["speaker_id"]: s for s in conversation["speakers"]}
    assert by_id[target["id"]]["name"] == "Mom"
    assert all(s["speaker_id"] is not None for s in conversation["speakers"])

    detail = (
        await client.get(f"/v1/conversations/{conversation['id']}", headers=account.headers)
    ).json()
    named = [t for t in detail["transcripts"] if t["speaker_id"] == target["id"]]
    assert named and all(t["speaker_name"] == "Mom" for t in named)
