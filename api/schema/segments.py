"""Response models for resource segments.

``SegmentRead`` is a discriminated union on ``resource`` — the same open-union
shape as the timeline events: each resource type serves its own typed
``attrs``, and adding a resource means adding a member here.
"""

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from typing_extensions import TypeAliasType

from database.schema.segments import ResourceSegment, SessionResourceSummary
from resources.audio import AudioAttrs


class SegmentReadBase(BaseModel):
    """One segment's metadata as served over HTTP.

    Carries no ``bucket``/``object_key`` (and no inline payload); a segment's
    stored bytes are fetched from ``GET /v1/segments/{id}/media``.
    """

    model_config = ConfigDict(frozen=True)

    id: UUID
    session_id: UUID
    sequence: int
    ingested_at: datetime
    captured_at: datetime | None
    offset_ms: int | None
    duration_ms: int | None
    byte_size: int
    content_type: str
    checksum_sha256: str | None = Field(description="Hex-encoded SHA-256 of the wire payload.")
    metadata: dict[str, Any]


class AudioSegmentRead(SegmentReadBase):
    resource: Literal["audio"] = "audio"
    attrs: AudioAttrs


class LocationSegmentAttrs(BaseModel):
    model_config = ConfigDict(frozen=True)

    points: int


class LocationSegmentRead(SegmentReadBase):
    resource: Literal["location"] = "location"
    attrs: LocationSegmentAttrs


# TypeAliasType keeps the union's name in generated schemas: Page[SegmentRead]
# stays Page_SegmentRead_ rather than a mangled Annotated[...] spelling.
SegmentRead = TypeAliasType(
    "SegmentRead",
    Annotated[AudioSegmentRead | LocationSegmentRead, Field(discriminator="resource")],
)


def segment_read(segment: ResourceSegment) -> AudioSegmentRead | LocationSegmentRead:
    base = dict(
        id=segment.id,
        session_id=segment.session_id,
        sequence=segment.sequence,
        ingested_at=segment.ingested_at,
        captured_at=segment.captured_at,
        offset_ms=segment.offset_ms,
        duration_ms=segment.duration_ms,
        byte_size=segment.byte_size,
        content_type=segment.content_type,
        checksum_sha256=segment.checksum_hex,
        metadata=segment.metadata,
    )
    if segment.resource == "location":
        points = len((segment.payload or {}).get("points", ()))
        return LocationSegmentRead(attrs=LocationSegmentAttrs(points=points), **base)
    return AudioSegmentRead(attrs=AudioAttrs.from_attrs(segment.attrs), **base)


class SessionResourceRead(BaseModel):
    """What one session holds of one resource."""

    model_config = ConfigDict(frozen=True)

    resource: str
    segments: int
    total_bytes: int
    first_captured_at: datetime | None
    last_captured_at: datetime | None

    @classmethod
    def from_model(cls, summary: SessionResourceSummary) -> "SessionResourceRead":
        return cls(
            resource=summary.resource,
            segments=summary.segments,
            total_bytes=summary.total_bytes,
            first_captured_at=summary.first_captured_at,
            last_captured_at=summary.last_captured_at,
        )
