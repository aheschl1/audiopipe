"""Client for any OpenAI-compatible chat endpoint that accepts images.

OpenAI itself, vLLM, llama-server and Ollama all speak ``POST
{base}/chat/completions`` with ``image_url`` parts carrying a data URL, which
is all this uses.
"""

import base64
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from processing.base import ServiceUnavailable
from processing.config import ProcessingSettings

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


class LlmError(Exception):
    """The model answered, but not usefully."""


class LlmUnavailable(LlmError, ServiceUnavailable):
    """The endpoint is unreachable, overloaded or rate-limiting."""


@dataclass(frozen=True)
class Image:
    jpeg: bytes
    caption: str  # a text part placed just before the image, e.g. its label


@dataclass(frozen=True)
class Reply:
    body: dict[str, Any]
    model: str
    usage: dict[str, int] = field(default_factory=dict)


def parse_json_object(content: str) -> dict[str, Any]:
    """The reply's JSON object, tolerating code fences and stray prose."""
    raw = _FENCE.sub("", content.strip())
    for candidate in (raw, raw[raw.find("{") : raw.rfind("}") + 1]):
        try:
            body = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(body, dict):
            return body
    raise LlmError(f"no JSON object in reply: {content[:300]}")


class Llm:
    def __init__(self, settings: ProcessingSettings) -> None:
        self._settings = settings
        headers = {}
        if settings.llm_api_key:
            headers["authorization"] = f"Bearer {settings.llm_api_key}"
        self._client = httpx.AsyncClient(
            base_url=settings.llm_base_url.rstrip("/"),
            headers=headers,
            timeout=httpx.Timeout(settings.llm_timeout_seconds, connect=10.0),
        )

    async def close(self) -> None:
        await self._client.aclose()

    def request(self, system: str, text: str, images: Sequence[Image]) -> dict[str, Any]:
        s = self._settings
        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        for image in images:
            url = {"url": "data:image/jpeg;base64," + base64.b64encode(image.jpeg).decode()}
            if s.llm_image_detail:
                url["detail"] = s.llm_image_detail
            content += [{"type": "text", "text": image.caption}, {"type": "image_url", "image_url": url}]
        body: dict[str, Any] = {
            "model": s.llm_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
        }
        if s.llm_json_mode:
            body["response_format"] = {"type": "json_object"}
        if s.llm_reasoning_effort:
            body["reasoning_effort"] = s.llm_reasoning_effort
        return body

    async def complete_json(self, system: str, text: str, images: Sequence[Image] = ()) -> Reply:
        try:
            response = await self._client.post(
                "/chat/completions", json=self.request(system, text, images)
            )
        except httpx.TransportError as exc:
            raise LlmUnavailable(f"llm unreachable: {exc}") from exc
        # Out of credit is a 429 too, but waiting won't fix it: count the attempt
        # so the job dies instead of being refunded forever.
        if response.status_code == 429 and "insufficient_quota" in response.text:
            raise LlmError(f"llm quota exhausted: {response.text[:300]}")
        if response.status_code == 429 or response.status_code >= 500:
            raise LlmUnavailable(f"llm answered {response.status_code}: {response.text[:300]}")
        if response.status_code != 200:
            raise LlmError(f"llm answered {response.status_code}: {response.text[:300]}")
        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LlmError(f"unexpected llm response: {response.text[:300]}") from exc
        if not isinstance(content, str):
            raise LlmError(f"no content in llm response: {response.text[:300]}")
        usage = body.get("usage")
        return Reply(
            body=parse_json_object(content),
            model=str(body.get("model") or self._settings.llm_model),
            usage={k: v for k, v in (usage or {}).items() if isinstance(v, int)}
            if isinstance(usage, dict)
            else {},
        )
