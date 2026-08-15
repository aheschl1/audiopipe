"""Demo pipeline: post-processing that consumes no segments.

Fed only by chaining (session-stats' callback); its input is the upstream
pipeline's artifact — the transcript-post-processing shape.
"""

from collections.abc import Sequence
from typing import Any

from database.pipe import DatabasePipe
from database.schema.jobs import Job, JobCreate
from processing.base import Pipeline


class StatsEchoPipeline(Pipeline):
    # Session-scoped like its parent: it echoes stats, whatever resources
    # they cover.
    name = "stats-echo"

    async def discover(self, limit: int) -> Sequence[JobCreate]:
        return ()  # chained-only: work arrives via another pipeline's callback

    async def process(self, job: Job) -> dict[str, Any]:
        artifact = None
        if job.session_id is not None:
            async with DatabasePipe() as pipe:
                artifact = await pipe.artifacts.find(
                    "session-stats", "session-stats", job.session_id
                )
        return {
            "echoed": job.payload,
            "artifact_id": str(artifact.id) if artifact else None,
            "artifact_metadata": artifact.metadata if artifact else None,
        }
