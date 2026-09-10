"""Sidecar outages must surface as ServiceUnavailable, not a plain retryable
error — a plain error burns the attempt budget in seconds while the sidecar
needs minutes to come back (10 diarize jobs dead-lettered this way, 2026-09)."""

import httpx
import pytest

from processing.base import ServiceUnavailable
from processing.classifier import Classifier, ClassifierError, ClassifierUnavailable
from processing.config import ProcessingSettings
from processing.diarizer import Diarizer, DiarizerError, DiarizerUnavailable


def _with_transport(client, handler) -> None:
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _refuse(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused")


def _loading(request: httpx.Request) -> httpx.Response:
    return httpx.Response(503, text="model loading")


def _server_error(request: httpx.Request) -> httpx.Response:
    return httpx.Response(500, text="boom")


@pytest.mark.parametrize(
    ("handler", "expected"),
    [(_refuse, DiarizerUnavailable), (_loading, DiarizerUnavailable), (_server_error, DiarizerError)],
)
async def test_diarizer_outage_mapping(handler, expected) -> None:
    diarizer = Diarizer(ProcessingSettings())
    _with_transport(diarizer, handler)
    with pytest.raises(expected) as excinfo:
        await diarizer.diarize(b"wav")
    assert isinstance(excinfo.value, ServiceUnavailable) == (expected is DiarizerUnavailable)
    await diarizer.close()


@pytest.mark.parametrize(
    ("handler", "expected"),
    [(_refuse, ClassifierUnavailable), (_loading, ClassifierUnavailable), (_server_error, ClassifierError)],
)
async def test_classifier_outage_mapping(handler, expected) -> None:
    classifier = Classifier(ProcessingSettings())
    _with_transport(classifier, handler)
    with pytest.raises(expected) as excinfo:
        await classifier.analyze(b"wav")
    assert isinstance(excinfo.value, ServiceUnavailable) == (expected is ClassifierUnavailable)
    await classifier.close()
