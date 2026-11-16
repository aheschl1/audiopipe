"""Image: blob-stored stills (photos from a camera), one per segment.

The format is sniffed from the leading bytes; a declared content type must
agree with it. Every image is a moment, so ``captured_at`` is required.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from resources.base import Resource, ResourceType, ResourceValidationError, ValidatedChunk


class ImageAttrs(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)


def sniff_image(data: bytes) -> str | None:
    """The image content type the bytes carry, or None if unrecognised."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"mif1", b"msf1"):
        return "image/heic"
    return None


def check_sniffed(sniffed: str | None, declared: str | None, kind: str) -> str:
    """Reconcile a sniffed and a declared content type (shared with video)."""
    if sniffed is None:
        raise ResourceValidationError(f"payload is not a supported {kind} format")
    if declared is not None and declared.split(";")[0].strip().lower() != sniffed:
        raise ResourceValidationError(f"declared {declared} but the payload is {sniffed}")
    return sniffed


class ImageResource(ResourceType):
    name = Resource.IMAGE
    storage = "blob"
    default_content_type = "image/jpeg"
    timeline_events = True
    uploadable = True

    def validate_chunk(
        self,
        payload: bytes,
        *,
        content_type: str | None,
        declared_attrs: Mapping[str, Any],
        captured_at: datetime | None,
        duration_ms: int | None,
    ) -> ValidatedChunk:
        content_type = check_sniffed(sniff_image(payload), content_type, "image")
        if captured_at is None:
            raise ResourceValidationError("an image needs captured_at")
        if duration_ms is not None:
            raise ResourceValidationError("an image has no duration")
        try:
            attrs = ImageAttrs.model_validate(dict(declared_attrs))
        except ValidationError as exc:
            raise ResourceValidationError(f"invalid image attrs: {exc}") from exc
        return ValidatedChunk(
            attrs=attrs.model_dump(exclude_none=True),
            payload=None,
            content_type=content_type,
            captured_at=captured_at,
            duration_ms=None,
        )
