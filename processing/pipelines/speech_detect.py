"""Tier 1: find the speech in every ended session.

Walks the session's PCM timeline through a VAD backend (processing/vad.py) and
records one ``speech-span`` artifact per detected span — the unit of work the
transcription tier consumes — plus a ``speech-map`` summary. Sessions whose
audio cannot be placed on a timeline (mixed formats, non-16k-mono, no audio)
are recorded as skipped rather than failed: retrying cannot help them.
"""

import logging
from collections.abc import Sequence
from typing import Any

from database.pipe import DatabasePipe
from database.repos.pipelines.common import PipelineDiscovery
from database.schema.artifacts import ArtifactCreate
from database.schema.jobs import Job, JobCreate
from processing import vad
from processing.base import Pipeline
from processing.config import get_settings
from resources import Resource
from services import stitch, timeline

logger = logging.getLogger(__name__)

_WINDOW_MS = 30_000


class SpeechDetectPipeline(Pipeline):
    name = "speech-detect"
    resource = Resource.AUDIO

    async def setup(self) -> None:
        self._settings = get_settings()
        self._backend = vad.build_backend(self._settings)

    async def discover(self, limit: int) -> Sequence[JobCreate]:
        async with DatabasePipe() as pipe:
            discovery = PipelineDiscovery(pipe.connection)
            sessions = await discovery.ended_sessions_with_resource_without(
                self.name, self.resource, limit
            )
        logging.debug(f"pipeline {self.name} found {len(sessions)} jobs")
        return tuple(
            JobCreate(
                pipeline=self.name,
                session_id=session.id,
                dedup_key=f"{self.name}:session:{session.id}",
            )
            for session in sessions
        )

    async def process(self, job: Job) -> dict[str, Any]:
        assert job.session_id is not None  # every job here comes from discover()
        try:
            line = await timeline.load_timeline(job.session_id)
        except (stitch.NotStitchable, timeline.NotRenderable) as exc:
            return await self._skip(job, str(exc))
        if line is None:
            return await self._skip(job, "session holds no audio")
        if (
            line.sample_rate_hz != vad.REQUIRED_SAMPLE_RATE
            or line.channels != vad.REQUIRED_CHANNELS
        ):
            return await self._skip(
                job,
                f"VAD needs {vad.REQUIRED_SAMPLE_RATE}Hz mono, session is "
                f"{line.sample_rate_hz}Hz/{line.channels}ch",
            )

        settings = self._settings
        self._backend.reset()
        scores: list[float] = []
        async for _, pcm in timeline.iter_windows(line, _WINDOW_MS):
            scores.extend(self._backend.scores(pcm))
        spans = vad.spans_from_scores(
            scores,
            total_ms=line.total_ms,
            threshold=settings.vad_threshold,
            min_speech_ms=settings.vad_min_speech_ms,
            merge_gap_ms=settings.vad_merge_gap_ms,
            pad_ms=settings.vad_pad_ms,
            max_span_ms=settings.vad_max_span_ms,
        )

        speech_ms = sum(span.end_ms - span.start_ms for span in spans)
        async with DatabasePipe() as pipe:
            # A requeued job (worker crash, dead-job recovery) must not append
            # a second generation of spans: replace, like the tiers below do.
            await pipe.artifacts.delete_for_pipeline(job.session_id, self.name)
            for span in spans:
                await pipe.artifacts.create(
                    ArtifactCreate(
                        pipeline=self.name,
                        kind="speech-span",
                        session_id=job.session_id,
                        start_ms=span.start_ms,
                        end_ms=span.end_ms,
                        metadata={"confidence": span.confidence},
                    )
                )
            await pipe.artifacts.create(
                ArtifactCreate(
                    pipeline=self.name,
                    kind="speech-map",
                    session_id=job.session_id,
                    metadata=self._map_metadata(
                        spans=len(spans), speech_ms=speech_ms, total_ms=line.total_ms
                    ),
                )
            )
        return {"spans": len(spans), "speech_ms": speech_ms, "total_ms": line.total_ms}

    async def _skip(self, job: Job, reason: str) -> dict[str, Any]:
        """Record an empty speech map so the session reads as processed."""
        logger.info("speech-detect skipping session %s: %s", job.session_id, reason)
        async with DatabasePipe() as pipe:
            await pipe.artifacts.delete_for_pipeline(job.session_id, self.name)
            await pipe.artifacts.create(
                ArtifactCreate(
                    pipeline=self.name,
                    kind="speech-map",
                    session_id=job.session_id,
                    metadata=self._map_metadata(
                        spans=0, speech_ms=0, total_ms=0, skipped=reason
                    ),
                )
            )
        return {"skipped": reason}

    def _map_metadata(self, **fields: Any) -> dict[str, Any]:
        settings = self._settings
        return {
            **fields,
            "backend": settings.vad_backend,
            "params": {
                "threshold": settings.vad_threshold,
                "min_speech_ms": settings.vad_min_speech_ms,
                "merge_gap_ms": settings.vad_merge_gap_ms,
                "pad_ms": settings.vad_pad_ms,
                "max_span_ms": settings.vad_max_span_ms,
            },
        }
