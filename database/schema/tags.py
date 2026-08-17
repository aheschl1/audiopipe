from datetime import datetime
from typing import Any
from uuid import UUID
from pydantic import BaseModel, ConfigDict

class TagWindowHit(BaseModel):
    """One window where the class scored at least the floor."""

    model_config = ConfigDict(frozen=True)

    artifact_id: UUID
    session_id: UUID
    start_ms: int
    end_ms: int
    label: str
    score: float
    metadata: dict[str, Any]
    created_at: datetime
    occurred_at: datetime | None = None  # wall-clock moment of start_ms
    session_label: str | None = None


class TagLabelCount(BaseModel):
    """One class that occurs in the caller's windows, with its footprint."""

    model_config = ConfigDict(frozen=True)

    label: str
    windows: int
    best: float