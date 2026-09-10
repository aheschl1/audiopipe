"""The diarization-service client seam.

The diarize tier depends on this contract, not on pyannote: ``POST
{base}/audio/diarizations`` (multipart) answering turns with block-local
speaker labels plus one embedding per detected speaker. The diar_pyannote
container implements it; anything else that speaks the same JSON can be
swapped in via ``PROCESSING_DIARIZER_BASE_URL``.

Turns may additionally carry ``overlap_ms``/``clean_ms`` (time shared with /
free of other speakers) and a per-turn ``embedding`` computed with overlapping
speech masked out. All three are optional — an older or degraded service
omits them and the tier behaves exactly as before per-turn support existed.
Per-turn vectors are what let the tier audit a label's purity (the diarizer can
wrongly put several people under one label; its per-label aggregate cannot
reveal that) and build voice-prints free of crosstalk frames.
"""

import logging
import math
from dataclasses import dataclass
from typing import Any

import httpx

from processing.base import ServiceUnavailable
from processing.config import ProcessingSettings

logger = logging.getLogger(__name__)


class DiarizerError(Exception):
    """The service failed or answered something unusable. Retryable."""


class DiarizerUnavailable(DiarizerError, ServiceUnavailable):
    """The service itself is down (unreachable or 503)."""


@dataclass(frozen=True, slots=True)
class Turn:
    start_ms: int
    end_ms: int
    speaker: str  # label local to this one request
    overlap_ms: int = 0  # time shared with other speakers' turns
    clean_ms: int | None = None  # non-overlapped time; None = service can't say
    embedding: tuple[float, ...] | None = None  # overlap masked out


@dataclass(frozen=True, slots=True)
class DiarizationResult:
    turns: tuple[Turn, ...]
    embeddings: dict[str, list[float]]  # per local speaker label
    model: str


class Diarizer:
    def __init__(self, settings: ProcessingSettings) -> None:
        self._base_url = settings.diarizer_base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.diarizer_timeout_seconds, connect=10.0)
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def diarize(self, wav: bytes, *, filename: str = "block.wav") -> DiarizationResult:
        try:
            response = await self._client.post(
                f"{self._base_url}/audio/diarizations",
                files={"file": (filename, wav, "audio/wav")},
            )
        except httpx.TransportError as exc:
            raise DiarizerUnavailable(f"diarizer unreachable: {exc}") from exc
        except httpx.HTTPError as exc:
            raise DiarizerError(f"diarizer request failed: {exc}") from exc
        if response.status_code == 503:
            raise DiarizerUnavailable(
                f"diarizer answered 503: {response.text[:500]}"
            )
        if response.status_code >= 400:
            raise DiarizerError(
                f"diarizer answered {response.status_code}: {response.text[:500]}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise DiarizerError(f"non-JSON diarizer response: {exc}") from exc
        return parse_response(body)


def _finite_vector(vector: Any) -> list[float] | None:
    """The vector as floats, or None if it is not a list of finite numbers.

    isfinite matters: json.loads parses a literal NaN into a float that
    would poison every pgvector distance downstream.
    """
    if not isinstance(vector, list) or not all(
        isinstance(value, (int, float)) and math.isfinite(value)
        for value in vector
    ):
        return None
    return [float(value) for value in vector]


def parse_response(body: Any) -> DiarizationResult:
    """Validate the service's JSON into a result; DiarizerError when unusable."""
    if not isinstance(body, dict) or not isinstance(body.get("turns"), list):
        raise DiarizerError(f"no turns in diarizer response: {body!r}")
    try:
        turns = []
        for t in body["turns"]:
            clean_ms = t.get("clean_ms")
            # Per-turn extras are optional (older service: absent) and never
            # fatal — a malformed vector degrades to None, exactly like a
            # turn too overlapped to embed.
            vector = _finite_vector(t.get("embedding"))
            turns.append(
                Turn(
                    int(t["start_ms"]),
                    int(t["end_ms"]),
                    str(t["speaker"]),
                    overlap_ms=int(t.get("overlap_ms") or 0),
                    clean_ms=None if clean_ms is None else int(clean_ms),
                    embedding=None if vector is None else tuple(vector),
                )
            )
        turns = tuple(turns)
    except (KeyError, TypeError, ValueError) as exc:
        raise DiarizerError(f"malformed turn in diarizer response: {exc}") from exc

    raw_embeddings = body.get("embeddings") or {}
    if not isinstance(raw_embeddings, dict):
        raise DiarizerError(f"malformed embeddings in diarizer response: {raw_embeddings!r}")
    embeddings: dict[str, list[float]] = {}
    for speaker, vector in raw_embeddings.items():
        # A malformed vector (the diarizer can emit NaN-laden embeddings for a
        # speaker with almost no clean speech) is not worth retrying to
        # death over: the turns are the load-bearing output — this tier
        # gates transcription — so drop just that speaker's embedding.
        parsed = _finite_vector(vector)
        if parsed is None:
            logger.warning("dropping malformed embedding for %r", speaker)
            continue
        embeddings[str(speaker)] = parsed

    return DiarizationResult(
        turns=turns, embeddings=embeddings, model=str(body.get("model", ""))
    )
