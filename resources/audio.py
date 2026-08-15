"""Audio: blob-stored, renderable, stitchable — the original resource.

Chunks are self-contained audio objects (whole short WAV/Opus/WebM files, not
bitstream slices); the bytes go to the object store untouched. PCM parameters
travel as attrs rather than dedicated columns.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from resources.base import Resource, ResourceType, ResourceValidationError, ValidatedChunk


class AudioAttrs(BaseModel):
    """PCM parameters as declared by the client (all optional)."""

    model_config = ConfigDict(frozen=True)

    codec: str | None = None
    sample_rate_hz: int | None = Field(default=None, gt=0)
    channels: int | None = Field(default=None, gt=0)

    @classmethod
    def from_attrs(cls, attrs: Mapping[str, Any]) -> "AudioAttrs":
        """Read a stored segment's ``attrs`` — lenient, like the old columns."""
        return cls(
            codec=attrs.get("codec"),
            sample_rate_hz=attrs.get("sample_rate_hz"),
            channels=attrs.get("channels"),
        )


class AudioResource(ResourceType):
    name = Resource.AUDIO
    storage = "blob"
    default_content_type = "application/octet-stream"
    renderable = True
    stitchable = True

    def validate_chunk(
        self,
        payload: bytes,
        *,
        content_type: str | None,
        declared_attrs: Mapping[str, Any],
        captured_at: datetime | None,
        duration_ms: int | None,
    ) -> ValidatedChunk:
        # Any content type is accepted, as it always was; format problems
        # surface at stitch/render time, not ingest.
        try:
            attrs = AudioAttrs.model_validate(dict(declared_attrs))
        except ValidationError as exc:
            raise ResourceValidationError(f"invalid audio attrs: {exc}") from exc
        return ValidatedChunk(
            attrs=attrs.model_dump(exclude_none=True),
            payload=None,
            content_type=content_type or self.default_content_type,
            captured_at=captured_at,
            duration_ms=duration_ms,
        )
