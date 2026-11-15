"""Per-segment image/video derivatives: browser-playable proxies and thumbnails.

Each job is one segment (its id rides in the payload). Outputs are blob
artifacts linked to the segment (``links.segment``) and carry their own
``content_type``/``byte_size`` so the artifact media route can serve them
with ranges. Originals are never touched.
"""

import asyncio
import logging
from abc import abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar
from uuid import UUID

from database.pipe import DatabasePipe
from database.repos.pipelines.visual import VisualQueries, segment_dedup_key
from database.schema.artifacts import ArtifactCreate
from database.schema.jobs import Job, JobCreate
from database.schema.segments import ResourceSegment
from processing.base import Pipeline
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

    async def process(self, job: Job) -> dict[str, Any]:
        segment_id = UUID(job.payload["segment_id"])
        async with DatabasePipe() as pipe:
            segment = await pipe.segments.get(segment_id)
        if segment is None or segment.object_key is None:
            return {"missing": True}
        async with BlobPipe() as blobs:
            data = await blobs.get(segment.object_key)
        outputs = await asyncio.to_thread(self.render, segment, data)

        artifacts = []
        async with BlobPipe() as blobs:
            for output in outputs:
                key = pipeline_key(self.name, segment.session_id, f"{segment.id}/{output.name}")
                info = await blobs.put(key, output.data, output.content_type)
                artifacts.append(
                    ArtifactCreate(
                        pipeline=self.name,
                        kind=output.kind,
                        session_id=segment.session_id,
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
            await pipe.artifacts.create_many(artifacts)
        return {"segment_id": str(segment.id), "artifacts": len(artifacts)}

    @abstractmethod
    def render(self, segment: ResourceSegment, data: bytes) -> list[Output]:
        """CPU-bound transform of the segment's bytes; runs in a thread."""


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
    resource = Resource.VIDEO
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
