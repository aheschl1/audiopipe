"""Pydantic models for the ``resource_segments`` table.

A row is one ingested chunk of one resource. Blob-stored resources (audio)
keep their bytes in the object store under ``object_key``; inline resources
(location) carry their parsed payload in ``payload``. Exactly one of the two
shapes holds per row — the table CHECK enforces it.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from resources import Resource


class ResourceSegment(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    session_id: UUID
    user_id: UUID
    resource: Resource
    sequence: int
    ingested_at: datetime
    captured_at: datetime | None = None
    offset_ms: int | None = None
    duration_ms: int | None = None

    bucket: str | None = None
    object_key: str | None = None
    payload: Any | None = None
    byte_size: int
    content_type: str
    checksum_sha256: bytes | None = None

    attrs: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def checksum_hex(self) -> str | None:
        return self.checksum_sha256.hex() if self.checksum_sha256 else None


class SegmentCreate(BaseModel):
    """Input for registering a segment (blob already written, or inline).

    ``sequence`` may be left ``None``, in which case the repository assigns the
    next one within the session.
    """

    model_config = ConfigDict(frozen=True)

    id: UUID
    session_id: UUID
    user_id: UUID
    resource: Resource = Resource.AUDIO
    byte_size: int
    content_type: str = "application/octet-stream"
    bucket: str | None = None
    object_key: str | None = None
    payload: Any | None = None
    sequence: int | None = None
    captured_at: datetime | None = None
    offset_ms: int | None = None
    duration_ms: int | None = None
    checksum_sha256: bytes | None = None
    attrs: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ResourceUsage(BaseModel):
    """How much of one resource a user has stored."""

    model_config = ConfigDict(frozen=True)

    resource: str
    segments: int
    total_bytes: int


class SessionResourceSummary(BaseModel):
    """What one session holds of one resource."""

    model_config = ConfigDict(frozen=True)

    resource: str
    segments: int
    total_bytes: int
    first_captured_at: datetime | None = None
    last_captured_at: datetime | None = None


class SegmentSetFingerprint(BaseModel):
    """A cheap change-token for one session's segment set of one resource.

    Segments are only appended or deleted, never mutated, so any change moves
    at least one of these fields. Scoped to a single resource: equal audio
    fingerprints must mean the stitched audio is byte-for-byte identical, so
    interleaved rows of other resources cannot participate. Read with a single
    aggregate query -- no rows cross the wire.
    """

    model_config = ConfigDict(frozen=True)

    count: int
    max_sequence: int
    total_bytes: int
