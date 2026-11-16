"""Automation rules and their runs, as the API returns them."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from database.schema.automations import Automation, AutomationWrite, RunListing, RunStatus


class AutomationRead(AutomationWrite):
    id: UUID
    active_since: datetime = Field(description="Only moments created after this are considered.")
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, automation: Automation) -> "AutomationRead":
        return cls.model_validate(automation.model_dump(exclude={"user_id"}))


class AutomationRunRead(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    automation_id: UUID
    automation_name: str
    moment_id: UUID
    moment_title: str
    moment_start_at: datetime
    status: RunStatus
    fields: dict[str, str | None] = Field(description="What an extract action filled in.")
    reason: str | None = Field(description="Why the conditions did or didn't hold, when the model was asked.")
    error: str | None
    created_at: datetime

    @classmethod
    def from_model(cls, run: RunListing) -> "AutomationRunRead":
        out: dict[str, Any] = run.output
        return cls(
            id=run.id,
            automation_id=run.automation_id,
            automation_name=run.automation_name,
            moment_id=run.moment_id,
            moment_title=run.moment_title,
            moment_start_at=run.moment_start_at,
            status=run.status,
            fields=out.get("fields") or {},
            reason=out.get("reason"),
            error=run.error,
            created_at=run.created_at,
        )
