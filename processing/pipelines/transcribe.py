"""Tier: transcribe every diarized utterance.

Consumes ``utterance`` artifacts (the diarize tier's ASR units — same-speaker
turns merged to the model's input window), renders each from the session
timeline, sends it to the transcription service (processing/transcriber.py —
model and protocol are environment-swappable), and records a ``transcript``
artifact on the same range. Transcripts live entirely in the artifact row —
utterances are short, so the full text fits in metadata and no blob is
written.

Diarize invalidates this tier's output when it republishes a session: it
deletes the transcripts in the same transaction that replaces the utterances,
and the queued jobs pointing at deleted utterances skip themselves.
"""

from collections.abc import Sequence
import logging
from typing import Any

from database.pipe import DatabasePipe
from database.repos.pipelines.transcribe import TranscribeQueries
from database.schema.artifacts import ArtifactCreate
from database.schema.jobs import Job, JobCreate
from processing.base import Pipeline
from processing.config import get_settings
from processing.transcriber import Transcriber
from resources import Resource
from services import stitch, timeline

_SOURCE_PIPELINE = "diarize"


class TranscribePipeline(Pipeline):
    name = "transcribe"
    resource = Resource.AUDIO

    async def setup(self) -> None:
        self._settings = get_settings()
        self._transcriber = Transcriber(self._settings)

    async def teardown(self) -> None:
        await self._transcriber.close()

    async def discover(self, limit: int) -> Sequence[JobCreate]:
        async with DatabasePipe() as pipe:
            utterances = await TranscribeQueries(pipe.connection).utterances_without_jobs(
                self.name, _SOURCE_PIPELINE, limit
            )
        logging.debug(f"pipeline {self.name} found {len(utterances)} jobs")
        return tuple(
            JobCreate(
                pipeline=self.name,
                session_id=utterance.session_id,
                artifact_id=utterance.id,
                payload={
                    "start_ms": utterance.start_ms,
                    "end_ms": utterance.end_ms,
                    "speaker": utterance.metadata.get("speaker"),
                    "overlap_ms": utterance.metadata.get("overlap_ms"),
                },
                dedup_key=f"{self.name}:artifact:{utterance.id}",
            )
            for utterance in utterances
        )

    async def process(self, job: Job) -> dict[str, Any]:
        assert job.session_id is not None
        if job.artifact_id is None:
            # Diarize republished the session and deleted this utterance
            # (the FK nulls artifact_id); the replacement utterance has its
            # own job.
            return {"skipped": "utterance deleted before transcription"}
        start_ms, end_ms = job.payload["start_ms"], job.payload["end_ms"]
        speaker = job.payload.get("speaker")
        overlap_ms = job.payload.get("overlap_ms")

        try:
            line = await timeline.load_timeline(job.session_id)
            if line is None:
                return {"skipped": "session audio is gone"}
            clip = await timeline.render_range(line, start_ms, end_ms)
        except (stitch.NotStitchable, timeline.NotRenderable) as exc:
            # Retrying cannot make the audio renderable.
            return {"skipped": str(exc)}

        # TranscriberError raises through: the worker retries with backoff,
        # which is exactly right for a briefly-down service.
        result = await self._transcriber.transcribe(
            clip, filename=f"{job.session_id}-{start_ms}-{end_ms}.wav"
        )

        metadata: dict[str, Any] = {
            "text": result.text,
            "chars": len(result.text),
            "model": self._settings.transcriber_model,
            "speaker": speaker,
            # Copied from the utterance: overlapped speech is
            # always transcribed (overlap never gates ASR), but
            # "this transcript contains crosstalk" must stay
            # queryable without joining back to the utterance.
            "overlap_ms": overlap_ms,
        }
        if result.words is not None:
            # Session-absolute ms: the clip was rendered from start_ms.
            metadata["words"] = [
                {"w": w.word, "s": start_ms + w.start_ms, "e": start_ms + w.end_ms}
                for w in result.words
            ]
        if result.language is not None:
            metadata["language"] = result.language

        async with DatabasePipe() as pipe:
            # Re-check inside the insert transaction: diarize may have
            # replaced the utterance while we were transcribing. (A commit
            # racing diarize's delete by microseconds could still slip an
            # orphan through; add FOR KEY SHARE here if one is ever observed.)
            if await pipe.artifacts.get(job.artifact_id) is None:
                return {"skipped": "utterance deleted during transcription"}
            # retranscribe deletes job history and can race an in-flight
            # job into a second run for the same utterance; job dedup no
            # longer guards that, so the data does.
            if await TranscribeQueries(pipe.connection).transcript_exists(
                job.artifact_id
            ):
                return {"skipped": "transcript already exists"}
            await pipe.artifacts.create(
                ArtifactCreate(
                    pipeline=self.name,
                    kind="transcript",
                    session_id=job.session_id,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    links={"utterance": str(job.artifact_id)},
                    metadata=metadata,
                )
            )
        return {"chars": len(result.text), "span_ms": end_ms - start_ms}
