"""Request and response models for recording sessions."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from database.schema.sessions import RecordingSession


class SessionCreateRequest(BaseModel):
    """Body for starting a session; the owner comes from the bearer token."""

    model_config = ConfigDict(frozen=True)

    device: str | None = None
    label: str | None = None
    started_at: datetime | None = Field(
        default=None, description="Defaults to the moment of creation."
    )
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionLabelRequest(BaseModel):
    """Body of ``POST /sessions/{id}/label``. ``label`` is required so an
    empty body is a 422, never a silent clear; send an explicit null to
    remove the name and fall back to the device/id display."""

    model_config = ConfigDict(frozen=True)

    label: str | None = Field(
        description="The name to set, or null to clear it.", max_length=200
    )


class TranscriptEditRequest(BaseModel):
    """Body of ``POST /sessions/{id}/transcripts/{artifact_id}``. Empty text
    is rejected — correcting an utterance never silently erases it."""

    model_config = ConfigDict(frozen=True)

    text: str = Field(description="The corrected transcript text.", min_length=1)


class SplitAllResponse(BaseModel):
    """Result of ``POST /sessions/split``."""

    model_config = ConfigDict(frozen=True)

    split: int = Field(description="How many open sessions were ended.")


class SessionRead(BaseModel):
    """A recording session as served over HTTP.

    Carries no ``user_id``; every session a caller can see is their own.
    """

    model_config = ConfigDict(frozen=True)

    id: UUID
    device: str | None
    label: str | None
    started_at: datetime
    ended_at: datetime | None
    is_open: bool = Field(description="True while the session has no end time.")
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, session: RecordingSession) -> "SessionRead":
        return cls(
            id=session.id,
            device=session.device,
            label=session.label,
            started_at=session.started_at,
            ended_at=session.ended_at,
            is_open=session.is_open,
            metadata=session.metadata,
            created_at=session.created_at,
            updated_at=session.updated_at,
        )
