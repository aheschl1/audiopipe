"""Embed query text in CLAP's joint space, via the classifier sidecar.

Shared by the HTTP search route and the MCP ``search_audio`` tool — both
rank stored window embeddings against the vector this returns.
"""

import math

import httpx
from fastapi import HTTPException, status

from api.config import get_settings

BAD_GATEWAY = HTTPException(
    status_code=status.HTTP_502_BAD_GATEWAY,
    detail="the audio embedding service is unavailable",
)


async def embed_query(text: str) -> tuple[list[float], str]:
    """The query in CLAP's text space; raises :data:`BAD_GATEWAY` when the
    sidecar is down or answers garbage."""
    settings = get_settings()
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(settings.classifier_timeout_seconds, connect=5.0)
        ) as client:
            response = await client.post(
                f"{settings.classifier_base_url.rstrip('/')}/text/embeddings",
                json={"texts": [text]},
            )
    except httpx.HTTPError as exc:
        raise BAD_GATEWAY from exc
    if response.status_code >= 400:
        raise BAD_GATEWAY
    try:
        body = response.json()
        (vector,) = body["embeddings"]
        model = body["model"]
    except (ValueError, KeyError, TypeError):
        raise BAD_GATEWAY from None
    if (
        not isinstance(vector, list)
        or not vector
        or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in vector)
        or not isinstance(model, str)
    ):
        raise BAD_GATEWAY
    return vector, model
