"""The audio-classification-service client seam.

The audio-tag tier depends on this interface, not on any particular model or
container: ``POST {base}/audio/analyze`` (multipart WAV) answers per-window
tag scores plus an audio embedding per window, with window times relative to
the uploaded clip. Our own audio_tagger container (CED + CLAP) speaks it;
swapping backends is environment-only (``PROCESSING_CLASSIFIER_*``).
"""

import math
from dataclasses import dataclass
from typing import Any

import httpx

from processing.base import ServiceUnavailable
from processing.config import ProcessingSettings


class ClassifierError(Exception):
    """The service failed or answered something unusable. Retryable."""


class ClassifierUnavailable(ClassifierError, ServiceUnavailable):
    """The service itself is down (unreachable or 503)."""


@dataclass(frozen=True, slots=True)
class ClassifiedWindow:
    start_ms: int  # clip-relative; the caller adds its span offset
    end_ms: int
    labels: tuple[tuple[str, float], ...]  # (label, sigmoid score), best first
    embedding: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class Analysis:
    windows: tuple[ClassifiedWindow, ...]
    tagger_model: str
    embedding_model: str


class Classifier:
    def __init__(self, settings: ProcessingSettings) -> None:
        self._base_url = settings.classifier_base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.classifier_timeout_seconds, connect=10.0)
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def analyze(self, wav: bytes, *, filename: str = "clip.wav") -> Analysis:
        try:
            response = await self._client.post(
                f"{self._base_url}/audio/analyze",
                files={"file": (filename, wav, "audio/wav")},
            )
        except httpx.TransportError as exc:
            raise ClassifierUnavailable(f"classifier unreachable: {exc}") from exc
        except httpx.HTTPError as exc:
            raise ClassifierError(f"classifier request failed: {exc}") from exc
        if response.status_code == 503:
            raise ClassifierUnavailable(
                f"classifier answered 503: {response.text[:500]}"
            )
        if response.status_code >= 400:
            raise ClassifierError(
                f"classifier answered {response.status_code}: {response.text[:500]}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise ClassifierError(f"non-JSON classifier response: {exc}") from exc
        return parse_response(body)


def parse_response(body: Any) -> Analysis:
    """Validate the service answer strictly; anything off is a retryable error.

    Non-finite floats matter: ``json.loads`` parses literal ``NaN``, and one
    stored NaN would poison every pgvector distance it participates in.
    """
    if not isinstance(body, dict) or not isinstance(body.get("windows"), list):
        raise ClassifierError(f"unexpected classifier response shape: {body!r}")
    models = body.get("models")
    if not isinstance(models, dict):
        raise ClassifierError(f"no models in classifier response: {body!r}")
    tagger = models.get("tagger")
    embedder = models.get("clap")
    if not isinstance(tagger, str) or not isinstance(embedder, str):
        raise ClassifierError(f"unusable model ids in classifier response: {models!r}")

    windows = []
    for row in body["windows"]:
        if not isinstance(row, dict):
            raise ClassifierError(f"unusable window in classifier response: {row!r}")
        start, end = row.get("start_ms"), row.get("end_ms")
        labels, embedding = row.get("labels"), row.get("embedding")
        if (
            not isinstance(start, int)
            or not isinstance(end, int)
            or end <= start
            or not isinstance(labels, list)
            or not isinstance(embedding, list)
            or not embedding
        ):
            raise ClassifierError(f"unusable window in classifier response: {row!r}")
        if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in embedding):
            raise ClassifierError(f"non-finite embedding for window {start}..{end}ms")
        parsed = []
        for item in labels:
            label, score = (
                (item.get("label"), item.get("score")) if isinstance(item, dict) else (None, None)
            )
            if (
                not isinstance(label, str)
                or not isinstance(score, (int, float))
                or not math.isfinite(score)
            ):
                raise ClassifierError(f"unusable label in classifier response: {item!r}")
            parsed.append((label, float(score)))
        windows.append(
            ClassifiedWindow(
                start_ms=start,
                end_ms=end,
                labels=tuple(parsed),
                embedding=tuple(float(v) for v in embedding),
            )
        )
    return Analysis(
        windows=tuple(windows), tagger_model=tagger, embedding_model=embedder
    )
