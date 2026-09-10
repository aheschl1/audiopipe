"""A sidecar outage must not dead-letter jobs: attempts are refunded."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import processing.worker as worker
from processing.base import ServiceUnavailable
from processing.config import ProcessingSettings


class _Pipe:
    def __init__(self, jobs):
        self.jobs = jobs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


async def test_service_unavailable_requeues_without_counting(monkeypatch) -> None:
    jobs = MagicMock()
    jobs.retry = AsyncMock()
    jobs.mark_dead = AsyncMock()
    monkeypatch.setattr(worker, "DatabasePipe", lambda: _Pipe(jobs))

    job = MagicMock()
    job.id = uuid4()
    job.attempts = 5
    job.max_attempts = 5

    pipeline = MagicMock()
    pipeline.name = "transcribe"
    pipeline.process = AsyncMock(side_effect=ServiceUnavailable("asr answered 503"))

    settings = ProcessingSettings()
    await worker._run_one(pipeline, job, settings)

    jobs.mark_dead.assert_not_awaited()
    jobs.retry.assert_awaited_once()
    kwargs = jobs.retry.await_args.kwargs
    assert kwargs["count_attempt"] is False
    run_at = jobs.retry.await_args.args[2]
    assert (run_at - datetime.now(UTC)).total_seconds() > 5


async def test_transcriber_unavailable_is_a_service_unavailable() -> None:
    from processing.transcriber import TranscriberError, TranscriberUnavailable

    assert issubclass(TranscriberUnavailable, ServiceUnavailable)
    assert issubclass(TranscriberUnavailable, TranscriberError)


async def test_diarizer_unavailable_is_a_service_unavailable() -> None:
    from processing.diarizer import DiarizerError, DiarizerUnavailable

    assert issubclass(DiarizerUnavailable, ServiceUnavailable)
    assert issubclass(DiarizerUnavailable, DiarizerError)


async def test_classifier_unavailable_is_a_service_unavailable() -> None:
    from processing.classifier import ClassifierError, ClassifierUnavailable

    assert issubclass(ClassifierUnavailable, ServiceUnavailable)
    assert issubclass(ClassifierUnavailable, ClassifierError)
