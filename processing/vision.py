"""Client for the vision sidecar: SigLIP 2 image and text embeddings in one space."""

import math
from collections.abc import Sequence
from typing import Any

import httpx

from processing.base import ServiceUnavailable
from processing.config import ProcessingSettings

_IMAGES_PER_REQUEST = 64


class VisionError(Exception):
    """The vision service answered, but not usefully."""


class VisionUnavailable(VisionError, ServiceUnavailable):
    """The vision service is unreachable or still loading."""


def parse_embeddings(body: Any, expected: int) -> tuple[list[list[float]], str]:
    """Strict: a NaN stored in pgvector poisons every distance it touches."""
    if not isinstance(body, dict) or not isinstance(body.get("model"), str):
        raise VisionError(f"unexpected vision response: {str(body)[:300]}")
    rows = body.get("embeddings")
    if not isinstance(rows, list) or len(rows) != expected:
        raise VisionError(f"expected {expected} embeddings, got {str(rows)[:100]}")
    for row in rows:
        if (
            not isinstance(row, list)
            or not row
            or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in row)
        ):
            raise VisionError("unusable embedding in vision response")
    return [[float(v) for v in row] for row in rows], body["model"]


class Vision:
    def __init__(self, settings: ProcessingSettings) -> None:
        self._base_url = settings.vision_base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.vision_timeout_seconds, connect=10.0)
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _post(self, path: str, **kwargs: Any) -> Any:
        try:
            response = await self._client.post(f"{self._base_url}{path}", **kwargs)
        except httpx.TransportError as exc:
            raise VisionUnavailable(f"vision service unreachable: {exc}") from exc
        if response.status_code == 503:
            raise VisionUnavailable(f"vision service answered 503: {response.text[:300]}")
        if response.status_code != 200:
            raise VisionError(f"vision service answered {response.status_code}: {response.text[:300]}")
        try:
            return response.json()
        except ValueError as exc:
            raise VisionError(f"non-JSON vision response: {exc}") from exc

    async def embed_images(self, jpegs: Sequence[bytes]) -> tuple[list[list[float]], str]:
        vectors: list[list[float]] = []
        model = ""
        for start in range(0, len(jpegs), _IMAGES_PER_REQUEST):
            chunk = jpegs[start : start + _IMAGES_PER_REQUEST]
            files = [("files", (f"{i}.jpg", data, "image/jpeg")) for i, data in enumerate(chunk)]
            rows, model = parse_embeddings(await self._post("/images/embeddings", files=files), len(chunk))
            vectors += rows
        return vectors, model
