"""Video: blob-stored clips, each independently playable.

A segment is a whole short file (an fMP4 with its own init boxes, or an
MPEG-TS run starting on a keyframe) — never a fragment that needs another
segment to decode, the same rule as audio chunks. Any audio track inside is
kept but is not the ``audio`` resource: audio pipelines never see it.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from resources.base import Resource, ResourceType, ResourceValidationError, ValidatedChunk
from resources.image import check_sniffed

_TS_PACKET = 188


class VideoAttrs(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    codec: str | None = None
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    fps: float | None = Field(default=None, gt=0)


def sniff_video(data: bytes) -> str | None:
    if data[4:8] == b"ftyp":
        return "video/mp4"
    if len(data) > _TS_PACKET and data[0] == 0x47 and data[_TS_PACKET] == 0x47:
        return "video/mp2t"
    return None


class VideoResource(ResourceType):
    name = Resource.VIDEO
    storage = "blob"
    default_content_type = "video/mp4"
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
        content_type = check_sniffed(sniff_video(payload), content_type, "video")
        if captured_at is None:
            raise ResourceValidationError("a video clip needs captured_at")
        if duration_ms is None or duration_ms <= 0:
            raise ResourceValidationError("a video clip needs a positive duration_ms")
        try:
            attrs = VideoAttrs.model_validate(dict(declared_attrs))
        except ValidationError as exc:
            raise ResourceValidationError(f"invalid video attrs: {exc}") from exc
        return ValidatedChunk(
            attrs=attrs.model_dump(exclude_none=True),
            payload=None,
            content_type=content_type,
            captured_at=captured_at,
            duration_ms=duration_ms,
        )
