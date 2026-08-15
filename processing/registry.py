"""The explicit pipeline registry, and where callbacks are wired.

Adding a pipeline is: write the class, import it here, append it to
``PIPELINES``, and (optionally) give it a callback in ``_callback_for``.
Callbacks are constructed in ``build()``, which runs inside the worker child
process — they are never pickled across the process boundary.
"""

from typing import Any

import resources
from database.pipe import DatabasePipe
from database.schema.jobs import Job, JobCreate
from processing.base import Callback, Pipeline
from processing.pipelines.audio_tag import AudioTagPipeline
from processing.pipelines.diarize import DiarizePipeline
from processing.pipelines.session_stats import SessionStatsPipeline
from processing.pipelines.speaker_cluster import SpeakerClusterPipeline
from processing.pipelines.sound_span import SoundSpanPipeline
from processing.pipelines.speech_detect import SpeechDetectPipeline
from processing.pipelines.stats_echo import StatsEchoPipeline
from processing.pipelines.transcribe import TranscribePipeline
from processing.pipelines.transcribe_ab import TranscribeAbPipeline

PIPELINES: tuple[type[Pipeline], ...] = (
    SessionStatsPipeline,
    StatsEchoPipeline,
    SpeechDetectPipeline,
    DiarizePipeline,
    TranscribePipeline,
    TranscribeAbPipeline,
    SpeakerClusterPipeline,
    AudioTagPipeline,
    SoundSpanPipeline,
)


def names() -> tuple[str, ...]:
    return tuple(cls.name for cls in PIPELINES)


def build(name: str) -> Pipeline:
    """Instantiate one pipeline with its wired callback (KeyError if unknown)."""
    cls = {c.name: c for c in PIPELINES}[name]
    return cls(callback=_callback_for(name))


def _callback_for(name: str) -> Callback | None:
    if name == SessionStatsPipeline.name:
        return enqueue_follow_up(StatsEchoPipeline.name)
    return None


def enqueue_follow_up(target: str) -> Callback:
    """Callback factory: on success, enqueue ``target`` with the parent's
    linkage - the transcription -> post-processing chaining shape."""

    async def _enqueue(job: Job, result: dict[str, Any]) -> None:
        async with DatabasePipe() as pipe:
            await pipe.jobs.enqueue(
                JobCreate(
                    pipeline=target,
                    session_id=job.session_id,
                    priority=job.priority,
                    payload={"source_job_id": str(job.id), "source_result": result},
                    dedup_key=f"{target}:from:{job.id}",
                )
            )

    return _enqueue


if len(set(names())) != len(PIPELINES):
    raise RuntimeError("duplicate pipeline names in PIPELINES")
if "users" in names():
    # The blob layout reserves users/ for segment storage (storage/keys.py).
    raise RuntimeError("'users' is a reserved name; pick another pipeline name")
for _cls in PIPELINES:
    # The Resource type makes a stray name statically improbable; this catches
    # an enum member the registry does not implement.
    if _cls.resource is not None and _cls.resource not in resources.names():
        raise RuntimeError(
            f"pipeline {_cls.name!r} declares unknown resource {_cls.resource!r}"
        )
