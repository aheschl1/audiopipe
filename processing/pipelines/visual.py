"""Per-segment image/video derivatives: proxies, thumbnails and keyframes.

Each job is one segment (its id rides in the payload). Outputs are blob
artifacts linked to the segment (``links.segment``) and carry their own
``content_type``/``byte_size`` so the artifact media route can serve them
with ranges. Originals are never touched.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import timedelta
from dataclasses import dataclass, field
from typing import Any, ClassVar
from uuid import UUID

from database.pipe import DatabasePipe
from database.repos.pipelines.visual import VisualQueries, segment_dedup_key
from database.schema.artifacts import ArtifactCreate, PipelineArtifact
from database.schema.embeddings import VisualEmbeddingCreate
from database.schema.jobs import Job, JobCreate
from database.schema.segments import ResourceSegment
import numpy as np

from processing.base import Pipeline
from processing.config import get_settings
from processing.keyframes import KeyframeParams, Pick, select
from processing.vision import Vision
from resources import Resource
from storage.keys import pipeline_key
from storage.pipe import BlobPipe

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Output:
    kind: str
    name: str
    data: bytes
    content_type: str
    metadata: dict[str, Any] = field(default_factory=dict)
    at_ms: int | None = None  # session-relative instant, when known


class VisualSegmentPipeline(Pipeline):
    resources: ClassVar[tuple[Resource, ...]]

    async def discover(self, limit: int) -> Sequence[JobCreate]:
        async with DatabasePipe() as pipe:
            segments = await VisualQueries(pipe.connection).segments_without_jobs(
                self.name, tuple(str(r) for r in self.resources), limit
            )
        return tuple(
            JobCreate(
                pipeline=self.name,
                session_id=segment.session_id,
                payload={"segment_id": str(segment.id)},
                dedup_key=segment_dedup_key(self.name, segment.id),
            )
            for segment in segments
        )

    async def _load(self, job: Job) -> tuple[ResourceSegment, bytes] | None:
        async with DatabasePipe() as pipe:
            segment = await pipe.segments.get(UUID(job.payload["segment_id"]))
        if segment is None or segment.object_key is None:
            return None
        async with BlobPipe() as blobs:
            return segment, await blobs.get(segment.object_key)

    async def _publish(
        self,
        segment: ResourceSegment,
        outputs: Sequence[Output],
        then: Callable[[DatabasePipe, list[PipelineArtifact]], Awaitable[Any]] | None = None,
    ) -> list[PipelineArtifact]:
        """Blobs first, then the artifact rows (and ``then``) in one transaction."""
        rows = []
        async with BlobPipe() as blobs:
            for output in outputs:
                # An instant, as the 1ms span the artifacts table requires;
                # frames from before the session started stay unplaced.
                placed = output.at_ms if output.at_ms is not None and output.at_ms >= 0 else None
                key = pipeline_key(self.name, segment.session_id, f"{segment.id}/{output.name}")
                info = await blobs.put(key, output.data, output.content_type)
                rows.append(
                    ArtifactCreate(
                        pipeline=self.name,
                        kind=output.kind,
                        session_id=segment.session_id,
                        start_ms=placed,
                        end_ms=None if placed is None else placed + 1,
                        bucket=info.bucket,
                        object_key=info.key,
                        links={"segment": str(segment.id)},
                        metadata={
                            **output.metadata,
                            "content_type": output.content_type,
                            "byte_size": len(output.data),
                        },
                    )
                )
        async with DatabasePipe() as pipe:
            # A rerun (worker killed before the job was marked done) replaces
            # the earlier set; the blob keys are the same, so only rows repeat.
            await pipe.artifacts.delete_for_segment(segment.session_id, self.name, segment.id)
            created = await pipe.artifacts.create_many(rows)
            if then is not None:
                await then(pipe, created)
        return created

    async def process(self, job: Job) -> dict[str, Any]:
        loaded = await self._load(job)
        if loaded is None:
            return {"missing": True}
        segment, data = loaded
        outputs = await asyncio.to_thread(self.render, segment, data)
        created = await self._publish(segment, outputs)
        return {"segment_id": str(segment.id), "artifacts": len(created)}

    def render(self, segment: ResourceSegment, data: bytes) -> list[Output]:
        """CPU-bound transform of the segment's bytes; runs in a thread."""
        raise NotImplementedError


class ThumbnailPipeline(VisualSegmentPipeline):
    """One small JPEG per image; evenly spaced frame stills per video clip."""

    name = "thumbnail"
    resources = (Resource.IMAGE, Resource.VIDEO)

    def render(self, segment: ResourceSegment, data: bytes) -> list[Output]:
        from processing import media

        if segment.resource == Resource.IMAGE:
            stills = [media.image_thumbnail(data)]
        else:
            stills = media.video_frames(data)
        return [
            Output(
                kind="thumbnail",
                name=f"thumb-{i:03d}.jpg",
                data=still.data,
                content_type="image/jpeg",
                metadata={
                    "width": still.width,
                    "height": still.height,
                    "offset_ms": still.offset_ms,
                },
            )
            for i, still in enumerate(stills)
        ]


class VideoProxyPipeline(VisualSegmentPipeline):
    """An upright 720p H.264/AAC MP4 of each clip, whatever the source codec."""

    name = "video-proxy"
    resources = (Resource.VIDEO,)

    def render(self, segment: ResourceSegment, data: bytes) -> list[Output]:
        from processing import media

        clip = media.proxy(data)
        return [
            Output(
                kind="video-proxy",
                name="proxy.mp4",
                data=clip.data,
                content_type="video/mp4",
                metadata={
                    "width": clip.width,
                    "height": clip.height,
                    "duration_ms": clip.duration_ms,
                },
            )
        ]


class KeyframePipeline(VisualSegmentPipeline):
    """The frames worth reading: every photo, and the novel moments of each clip.

    Each keyframe is a 1024px JPEG artifact placed on the session timeline,
    with its SigLIP embedding in ``visual_embeddings`` for text->frame search.
    Downstream visual tiers (OCR, captions) should work from these only.
    """

    name = "keyframe"
    resources = (Resource.IMAGE, Resource.VIDEO)

    async def setup(self) -> None:
        self._settings = get_settings()
        self._vision = Vision(self._settings)

    async def teardown(self) -> None:
        await self._vision.close()

    def _params(self) -> KeyframeParams:
        s = self._settings
        return KeyframeParams(
            novelty=s.keyframe_novelty,
            memory=s.keyframe_memory,
            settle_ms=s.keyframe_settle_ms,
            min_gap_ms=s.keyframe_min_gap_ms,
            heartbeat_ms=s.keyframe_heartbeat_ms,
            max_per_minute=s.keyframe_max_per_minute,
            blur_percentile=s.keyframe_blur_percentile,
        )

    async def process(self, job: Job) -> dict[str, Any]:
        from processing import media

        loaded = await self._load(job)
        if loaded is None:
            return {"missing": True}
        segment, data = loaded

        if segment.resource == Resource.IMAGE:
            still, candidate = await asyncio.to_thread(media.image_still, data)
            vectors, model = await self._vision.embed_images([candidate.jpeg])
            chosen = [(still, candidate, Pick(0, "photo", 1.0), vectors[0])]
        else:
            fps = self._settings.keyframe_candidate_fps
            candidates = await asyncio.to_thread(media.candidate_frames, data, fps)
            if not candidates:
                return {"segment_id": str(segment.id), "keyframes": 0}
            vectors, model = await self._vision.embed_images([c.jpeg for c in candidates])
            picks = select(candidates, np.asarray(vectors), self._params())
            offsets = [candidates[p.index].offset_ms for p in picks]
            stills = await asyncio.to_thread(media.frames_at, data, offsets)
            chosen = [
                (stills[candidates[p.index].offset_ms], candidates[p.index], p, vectors[p.index])
                for p in picks
                if candidates[p.index].offset_ms in stills
            ]

        base_ms = await self._wall_ms(segment)
        outputs = [
            Output(
                kind="keyframe",
                name=f"keyframe-{i:03d}.jpg",
                data=still.data,
                content_type="image/jpeg",
                at_ms=None if base_ms is None else base_ms + candidate.offset_ms,
                metadata={
                    "width": still.width,
                    "height": still.height,
                    "offset_ms": candidate.offset_ms,
                    "reason": pick.reason,
                    "novelty": round(pick.novelty, 4),
                    "sharpness": round(candidate.sharpness, 1),
                },
            )
            for i, (still, candidate, pick, _) in enumerate(chosen)
        ]

        async def embed(pipe: DatabasePipe, created: list[PipelineArtifact]) -> None:
            await pipe.visual_embeddings.create_many(
                [
                    VisualEmbeddingCreate(
                        artifact_id=artifact.id,
                        session_id=artifact.session_id,
                        model=model,
                        embedding=tuple(vector),
                    )
                    for artifact, (_, _, _, vector) in zip(created, chosen, strict=True)
                ]
            )

        await self._publish(segment, outputs, then=embed)
        return {
            "segment_id": str(segment.id),
            "keyframes": len(outputs),
            "reasons": [pick.reason for _, _, pick, _ in chosen],
            "model": model,
        }

    async def _wall_ms(self, segment: ResourceSegment) -> int | None:
        if segment.captured_at is None:
            return None
        async with DatabasePipe() as pipe:
            session = await pipe.sessions.get(segment.session_id)
        if session is None:
            return None
        return (segment.captured_at - session.started_at) // timedelta(milliseconds=1)
