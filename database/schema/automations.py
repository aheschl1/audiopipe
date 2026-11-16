"""Automations: if a new moment matches these conditions, then do these actions.

The rule's shape lives here so the API validates what it stores and the worker
reads it back the same way.
"""

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

Short = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class Conditions(BaseModel):
    """All set conditions must hold; the lists match when any entry does (case-insensitive)."""

    model_config = ConfigDict(frozen=True, extra="forbid", json_schema_serialization_defaults_required=True)

    people: list[Short] = Field(default_factory=list, max_length=20)
    topics: list[Short] = Field(default_factory=list, max_length=20)
    keywords: list[Short] = Field(
        default_factory=list, max_length=20,
        description="Matched against the title, summary and transcript.",
    )
    min_minutes: float | None = Field(None, ge=0, le=24 * 60)
    ask: str | None = Field(
        None, max_length=1000,
        description="A yes/no question the model answers about the moment, e.g. 'is a deadline agreed?'.",
    )

    @field_validator("ask")
    @classmethod
    def _blank_ask(cls, value: str | None) -> str | None:
        return value.strip() or None if value else None


class FieldSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", json_schema_serialization_defaults_required=True)

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
    description: str = Field("", max_length=500)


class ExtractAction(BaseModel):
    """Have the model fill these fields from the moment; they show on the moment page."""

    model_config = ConfigDict(frozen=True, extra="forbid", json_schema_serialization_defaults_required=True)

    type: Literal["extract"]
    fields: list[FieldSpec] = Field(min_length=1, max_length=20)

    @field_validator("fields")
    @classmethod
    def _unique(cls, fields: list[FieldSpec]) -> list[FieldSpec]:
        if len({f.name.lower() for f in fields}) != len(fields):
            raise ValueError("field names must be unique")
        return fields


# New action kinds join as a discriminated union on ``type``.
Action = ExtractAction


class AutomationWrite(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", json_schema_serialization_defaults_required=True)

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    enabled: bool = True
    trigger: Literal["moment"] = Field("moment", description="Runs on each new moment.")
    conditions: Conditions = Field(default_factory=Conditions)
    actions: list[Action] = Field(min_length=1, max_length=5)


class Automation(AutomationWrite):
    id: UUID
    user_id: UUID
    active_since: datetime
    created_at: datetime
    updated_at: datetime


RunStatus = Literal["matched", "skipped", "failed"]


class AutomationRun(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    automation_id: UUID
    moment_id: UUID
    status: RunStatus
    output: dict[str, Any]
    error: str | None
    created_at: datetime


class RunListing(AutomationRun):
    """A run joined with its rule's and moment's names, for the runs list."""

    automation_name: str
    moment_title: str
    moment_start_at: datetime


class PendingRun(BaseModel):
    model_config = ConfigDict(frozen=True)

    automation_id: UUID
    moment_id: UUID
    user_id: UUID
