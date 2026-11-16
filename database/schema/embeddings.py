"""Pydantic models for the ``speaker_embeddings``, ``audio_embeddings`` and
``visual_embeddings`` tables."""

import json
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator


class SpeakerEmbedding(BaseModel):
    """One speaker's voice embedding, keyed by its ``speaker-embedding``
    artifact. The vector is what the clustering tier compares; the artifact
    row carries the timeline addressing."""

    model_config = ConfigDict(frozen=True)

    artifact_id: UUID
    session_id: UUID
    speaker: str
    model: str
    embedding: tuple[float, ...]
    created_at: datetime

    @field_validator("embedding", mode="before")
    @classmethod
    def _parse_vector(cls, value: object) -> object:
        # pgvector's text form is '[1,2,3]' — valid JSON.
        if isinstance(value, str):
            return json.loads(value)
        return value


class SpeakerEmbeddingCreate(BaseModel):
    """Input for recording an embedding against an existing artifact row."""

    model_config = ConfigDict(frozen=True)

    artifact_id: UUID
    session_id: UUID
    speaker: str
    model: str
    embedding: tuple[float, ...]


class AudioEmbedding(BaseModel):
    """One audio window's CLAP embedding, keyed by its ``audio-tag`` artifact.
    The vector is what text->audio search compares; the artifact row carries
    the timeline addressing and the window's tag labels."""

    model_config = ConfigDict(frozen=True)

    artifact_id: UUID
    session_id: UUID
    model: str
    embedding: tuple[float, ...]
    created_at: datetime

    @field_validator("embedding", mode="before")
    @classmethod
    def _parse_vector(cls, value: object) -> object:
        # pgvector's text form is '[1,2,3]' — valid JSON.
        if isinstance(value, str):
            return json.loads(value)
        return value


class AudioEmbeddingCreate(BaseModel):
    """Input for recording a window embedding against an existing artifact."""

    model_config = ConfigDict(frozen=True)

    artifact_id: UUID
    session_id: UUID
    model: str
    embedding: tuple[float, ...]


class VisualEmbeddingCreate(BaseModel):
    """Input for recording a keyframe's image embedding against its artifact."""

    model_config = ConfigDict(frozen=True)

    artifact_id: UUID
    session_id: UUID
    model: str
    embedding: tuple[float, ...]
