"""The processing workers, end to end: a real ``python -m processing``
supervisor discovering, running, chaining, retrying, and deduplicating jobs.
"""

import asyncio
import json
import subprocess
import sys
import time

import httpx

from database.pipe import DatabasePipe
from database.schema.jobs import JobStatus
from database.schema.sessions import SessionCreate
from tests.e2e.conftest import (
    REPO_ROOT,
    TEST_BUCKET,
    Account,
    ingest,
    make_account,
    make_session,
    wait_for_job,
)
from tests.e2e.stub_audio_services import STUB_LANGUAGE, STUB_WORDS
from tests.wav import wav_bytes


async def _ended_session_with_segments(account: Account, count: int = 3):
    """A finished session holding `count` one-tenth-second segments."""
    session = await make_session(account)
    payloads = [wav_bytes(seconds=0.1) for _ in range(count)]
    for payload in payloads:
        await ingest(session.id, payload, duration_ms=100)
    return session, payloads


async def test_session_stats_discovers_processes_and_stores(
    worker: None, client: httpx.AsyncClient, account: Account, s3
) -> None:
    session, payloads = await _ended_session_with_segments(account)
    ended = await client.post(f"/v1/sessions/{session.id}/end", headers=account.headers)
    assert ended.status_code == 200

    job = await wait_for_job(session.id, "session-stats", JobStatus.SUCCEEDED)
    assert job.priority == 0
    assert job.dedup_key == f"session-stats:session:{session.id}"
    assert job.result == {
        "segments": len(payloads),
        "total_bytes": sum(len(p) for p in payloads),
        "duration_ms": 100 * len(payloads),
        "resources": {
            "audio": {
                "segments": len(payloads),
                "total_bytes": sum(len(p) for p in payloads),
                "duration_ms": 100 * len(payloads),
            }
        },
    }

    async with DatabasePipe() as pipe:
        artifact = await pipe.artifacts.find("session-stats", "session-stats", session.id)
    assert artifact is not None
    assert artifact.bucket == TEST_BUCKET
    assert artifact.metadata == job.result
    assert len(artifact.links["segments"]) == len(payloads)

    # The blob landed in the pipeline's own space and matches the result.
    key = f"session-stats/sessions/{session.id}/stats.json"
    assert artifact.object_key == key
    stored = json.loads(s3.get_object(Bucket=TEST_BUCKET, Key=key)["Body"].read())
    assert stored == job.result


async def test_callback_chains_to_artifact_consumer(worker: None, clean_state) -> None:
    account = await make_account()
    session, _ = await _ended_session_with_segments(account, count=1)
    async with DatabasePipe() as pipe:
        await pipe.sessions.end(session.id)

    parent = await wait_for_job(session.id, "session-stats", JobStatus.SUCCEEDED)
    echoed = await wait_for_job(session.id, "stats-echo", JobStatus.SUCCEEDED)

    # The chained job carries the parent's linkage and consumed no segments —
    # its input was the parent's artifact.
    assert echoed.payload["source_job_id"] == str(parent.id)
    assert echoed.payload["source_result"] == parent.result
    assert echoed.result is not None
    assert echoed.result["echoed"]["source_job_id"] == str(parent.id)
    assert echoed.result["artifact_metadata"] == parent.result


async def test_failing_job_retries_then_dies(worker: None, clean_state) -> None:
    account = await make_account()
    async with DatabasePipe() as pipe:
        session = await pipe.sessions.create(
            SessionCreate(
                user_id=account.user.id, device="glasses-01", metadata={"boom": True}
            )
        )
        await pipe.sessions.end(session.id)

    job = await wait_for_job(session.id, "session-stats", JobStatus.DEAD)
    assert job.attempts == 2  # PROCESSING_MAX_ATTEMPTS in conftest
    assert job.error is not None and "boom" in job.error
    assert job.finished_at is not None


async def test_diarized_utterances_are_transcribed(worker: None, clean_state) -> None:
    """The full chain: detect → diarize → transcribe, one transcript per
    utterance, pre-attributed to its speaker.

    The energy VAD backend (conftest env) treats the sine-tone segments as
    activity; the stub diarizer answers two 150ms turns for two speakers,
    which become two utterances; the transcribe tier renders each and sends
    it to the stub transcription service. Transcripts are rows only — no
    blob.
    """
    account = await make_account()
    session, payloads = await _ended_session_with_segments(account)
    async with DatabasePipe() as pipe:
        await pipe.sessions.end(session.id)

    detect = await wait_for_job(session.id, "speech-detect", JobStatus.SUCCEEDED)
    assert detect.result is not None and detect.result["spans"] == 1
    total_ms = 100 * len(payloads)
    assert detect.result["total_ms"] == total_ms

    diarized = await wait_for_job(session.id, "diarize", JobStatus.SUCCEEDED)
    assert diarized.result is not None and diarized.result["utterances"] == 2

    async with DatabasePipe() as pipe:
        utterances = await pipe.artifacts.list_for_session(session.id, kind="utterance")
    assert [
        (u.start_ms, u.end_ms, u.metadata["speaker"], u.metadata["turns"])
        for u in utterances
    ] == [
        (0, 150, "b0:SPEAKER_00", 1),
        (150, 300, "b0:SPEAKER_01", 1),
    ]

    await wait_for_job(session.id, "transcribe", JobStatus.SUCCEEDED)
    transcripts = []
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline and len(transcripts) < len(utterances):
        async with DatabasePipe() as pipe:
            transcripts = await pipe.artifacts.list_for_session(
                session.id, kind="transcript"
            )
        await asyncio.sleep(0.1)

    by_utterance = {t.links["utterance"]: t for t in transcripts}
    assert set(by_utterance) == {str(u.id) for u in utterances}
    for utterance in utterances:
        transcript = by_utterance[str(utterance.id)]
        assert (transcript.start_ms, transcript.end_ms) == (
            utterance.start_ms,
            utterance.end_ms,
        )
        assert transcript.metadata["text"] == "hello from the stub transcriber"
        assert transcript.metadata["speaker"] == utterance.metadata["speaker"]
        assert transcript.metadata["language"] == STUB_LANGUAGE
        # Word timings arrive clip-relative and land session-absolute.
        assert transcript.metadata["words"] == [
            {
                "w": word["word"],
                "s": utterance.start_ms + word["start_ms"],
                "e": utterance.start_ms + word["end_ms"],
            }
            for word in STUB_WORDS
        ]
        # Postgres-only: the row is the store.
        assert transcript.bucket is None and transcript.object_key is None

    async with DatabasePipe() as pipe:
        jobs = await pipe.jobs.list_for_session(session.id)
    transcribe_jobs = [j for j in jobs if j.pipeline == "transcribe"]
    assert {j.artifact_id for j in transcribe_jobs} == {u.id for u in utterances}
    assert all(j.payload["speaker"] for j in transcribe_jobs)


async def _wait_for_transcripts(session_id, count: int) -> list:
    deadline = time.monotonic() + 15.0
    transcripts: list = []
    while time.monotonic() < deadline and len(transcripts) < count:
        async with DatabasePipe() as pipe:
            transcripts = await pipe.artifacts.list_for_session(
                session_id, kind="transcript"
            )
        await asyncio.sleep(0.1)
    assert len(transcripts) == count, "transcripts did not appear in time"
    return transcripts


async def test_retranscribe_regenerates_transcripts(worker: None, clean_state) -> None:
    """``sessions retranscribe`` drops the tier's artifacts and job history;
    the running worker re-discovers every utterance and writes fresh rows."""
    account = await make_account()
    session, _ = await _ended_session_with_segments(account)
    async with DatabasePipe() as pipe:
        await pipe.sessions.end(session.id)
    await wait_for_job(session.id, "transcribe", JobStatus.SUCCEEDED)
    old_ids = {t.id for t in await _wait_for_transcripts(session.id, 2)}

    result = subprocess.run(
        [sys.executable, "-m", "cli.main", "sessions", "retranscribe",
         str(session.id), "--yes"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "2 transcript(s)" in result.stdout

    fresh = await _wait_for_transcripts(session.id, 2)
    assert {t.id for t in fresh}.isdisjoint(old_ids)
    assert all(t.metadata["words"] for t in fresh)


async def test_requeued_utterances_skip_existing_transcripts(
    worker: None, clean_state
) -> None:
    """Lost job history with surviving transcripts (a retranscribe racing an
    in-flight job) must not duplicate rows: the re-run skips on the
    data-level guard."""
    account = await make_account()
    session, _ = await _ended_session_with_segments(account)
    async with DatabasePipe() as pipe:
        await pipe.sessions.end(session.id)
    await wait_for_job(session.id, "transcribe", JobStatus.SUCCEEDED)
    originals = {t.id for t in await _wait_for_transcripts(session.id, 2)}

    async with DatabasePipe() as pipe:
        assert await pipe.jobs.delete_for_session(session.id, "transcribe") == 2

    deadline = time.monotonic() + 15.0
    jobs: list = []
    while time.monotonic() < deadline:
        async with DatabasePipe() as pipe:
            jobs = [
                j
                for j in await pipe.jobs.list_for_session(session.id)
                if j.pipeline == "transcribe"
            ]
        if len(jobs) == 2 and all(j.status == JobStatus.SUCCEEDED for j in jobs):
            break
        await asyncio.sleep(0.1)
    assert all(
        j.result == {"skipped": "transcript already exists"} for j in jobs
    ), jobs
    assert {t.id for t in await _wait_for_transcripts(session.id, 2)} == originals


async def test_speech_is_diarized_with_embeddings(worker: None, clean_state) -> None:
    """The diarize tier: one block, block-namespaced turns, pgvector rows.

    The stub diarization service answers two fixed 150ms turns for two
    speakers; the tier offsets them onto the session timeline, namespaces the
    labels per block, and stores one ``speaker_embeddings`` row per speaker.
    """
    account = await make_account()
    session, _ = await _ended_session_with_segments(account)
    async with DatabasePipe() as pipe:
        await pipe.sessions.end(session.id)

    job = await wait_for_job(session.id, "diarize", JobStatus.SUCCEEDED)
    assert job.result == {"blocks": 1, "turns": 2, "utterances": 2, "speakers": 2}

    async with DatabasePipe() as pipe:
        speech_map = await pipe.artifacts.find("speech-detect", "speech-map", session.id)
        turns = await pipe.artifacts.list_for_session(session.id, kind="speaker-turn")
        embeddings = await pipe.artifacts.list_for_session(
            session.id, kind="speaker-embedding"
        )
        vectors = await pipe.embeddings.list_for_session(session.id)
        diarize_map = await pipe.artifacts.find("diarize", "diarize-map", session.id)
    assert speech_map is not None and job.artifact_id == speech_map.id

    assert [(t.start_ms, t.end_ms, t.metadata["speaker"]) for t in turns] == [
        (0, 150, "b0:SPEAKER_00"),
        (150, 300, "b0:SPEAKER_01"),
    ]
    assert all(t.metadata["block_start_ms"] == 0 for t in turns)

    # One embedding row per (block, speaker); the vector lives in pgvector,
    # keyed by its artifact — no blob. (Equal spans order by random id: sort.)
    assert sorted(e.metadata["speaker"] for e in embeddings) == [
        "b0:SPEAKER_00",
        "b0:SPEAKER_01",
    ]
    by_artifact = {vector.artifact_id: vector for vector in vectors}
    for embedding in embeddings:
        assert "embedding" not in embedding.metadata
        assert embedding.bucket is None and embedding.object_key is None
        stored = by_artifact[embedding.id]
        assert stored.session_id == session.id
        assert stored.speaker == embedding.metadata["speaker"]
        assert stored.model == embedding.metadata["model"]
        assert len(stored.embedding) == embedding.metadata["dim"] == 4

    assert diarize_map is not None
    assert diarize_map.metadata["turns"] == 2 and diarize_map.metadata["blocks"] == 1
    assert diarize_map.metadata["utterances"] == 2


async def test_silent_session_yields_no_spans_and_no_transcription(
    worker: None, clean_state
) -> None:
    account = await make_account()
    session = await make_session(account)
    await ingest(session.id, wav_bytes(seconds=0.3, amplitude=0), duration_ms=300)
    async with DatabasePipe() as pipe:
        await pipe.sessions.end(session.id)

    job = await wait_for_job(session.id, "speech-detect", JobStatus.SUCCEEDED)
    assert job.result is not None and job.result["spans"] == 0

    # Diarize still consumes the (empty) speech-map and publishes an empty map.
    diarized = await wait_for_job(session.id, "diarize", JobStatus.SUCCEEDED)
    assert diarized.result is not None and diarized.result["turns"] == 0

    await asyncio.sleep(1.0)  # several transcribe discovery passes
    async with DatabasePipe() as pipe:
        speech_map = await pipe.artifacts.find("speech-detect", "speech-map", session.id)
        diarize_map = await pipe.artifacts.find("diarize", "diarize-map", session.id)
        turns = await pipe.artifacts.list_for_session(session.id, kind="speaker-turn")
        jobs = await pipe.jobs.list_for_session(session.id)
    assert speech_map is not None and speech_map.metadata["spans"] == 0
    assert diarize_map is not None and diarize_map.metadata["turns"] == 0
    assert turns == []
    assert [j.pipeline for j in jobs if j.pipeline == "transcribe"] == []


async def test_discovery_never_reprocesses_finished_work(
    worker: None, clean_state
) -> None:
    account = await make_account()
    session, _ = await _ended_session_with_segments(account, count=1)
    async with DatabasePipe() as pipe:
        await pipe.sessions.end(session.id)
    await wait_for_job(session.id, "session-stats", JobStatus.SUCCEEDED)

    # Several discovery passes later (poll interval is 0.2s), still one job.
    await asyncio.sleep(1.0)
    async with DatabasePipe() as pipe:
        jobs = await pipe.jobs.list_for_session(session.id)
    stats_jobs = [j for j in jobs if j.pipeline == "session-stats"]
    assert len(stats_jobs) == 1
