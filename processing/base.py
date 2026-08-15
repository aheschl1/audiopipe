"""The parent class every processing pipeline descends from.

A pipeline is a work definition, not a runtime: the worker loop
(``processing.worker``) runs it in its own OS process. Pipelines act over
sessions however they see fit — many segments, another pipeline's artifacts,
or no stored inputs at all — through queries they own
(``database/repos/pipelines/``). Each pipeline also owns the blob space
``{name}/...`` (``storage.keys.pipeline_key``) and records its outputs in the
shared ``pipeline_artifacts`` table.
"""

import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, ClassVar

from database.schema.jobs import Job, JobCreate
from processing.config import ProcessingSettings
from resources import Resource

logger = logging.getLogger(__name__)

# Invoked after a job's success is committed. Receives the job and its result.
type Callback = Callable[[Job, dict[str, Any]], Awaitable[None]]


def backoff(attempts: int, settings: ProcessingSettings) -> float:
    """Seconds until the next try: base doubling per attempt, capped."""
    return min(
        settings.retry_backoff_cap_seconds,
        settings.retry_backoff_base_seconds * 2 ** (attempts - 1),
    )


class Pipeline(ABC):
    """One kind of background processing, run as its own worker process."""

    # Registry key, ``processing_jobs.pipeline`` value, and blob-prefix segment.
    name: ClassVar[str]

    # The resource this pipeline moves over; None = session-scoped work.
    resource: ClassVar[Resource | None] = None

    def __init__(self, callback: Callback | None = None) -> None:
        self.callback = callback

    async def setup(self) -> None:
        """Runs once in the worker process before the first job.

        Load heavy resources (models, clients) here — never at module import,
        which must stay cheap.
        """

    async def teardown(self) -> None:
        """Graceful-shutdown counterpart of :meth:`setup`."""

    @abstractmethod
    async def discover(self, limit: int) -> Sequence[JobCreate]:
        """Find this pipeline's own work.

        Runs every worker pass. Each item must carry a ``dedup_key`` so
        re-discovering the same work is a no-op; the query itself should
        exclude already-enqueued work to stay cheap (the dedup index is the
        correctness backstop). Pipelines fed only by chaining return ``()``.
        """

    @abstractmethod
    async def process(self, job: Job) -> dict[str, Any]:
        """Do the work; the returned dict is persisted as the job's result."""

    async def maybe_callback(self, job: Job, result: dict[str, Any]) -> None:
        """Invoke the callback, if any, after a successful committed run.

        Best-effort and at-most-once: the job is already 'succeeded', so a
        callback exception is logged and swallowed — it can never fail or
        retry the job.
        """
        if self.callback is None:
            return
        try:
            await self.callback(job, result)
        except Exception:
            logger.exception("callback failed for %s job %s", self.name, job.id)
