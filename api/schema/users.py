"""Response models for the authenticated caller."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr

from database.schema.segments import ResourceUsage
from database.schema.users import User


class ResourceUsageRead(BaseModel):
    """How much of one resource the caller has stored."""

    model_config = ConfigDict(frozen=True)

    resource: str
    segments: int
    total_bytes: int

    @classmethod
    def from_model(cls, usage: ResourceUsage) -> "ResourceUsageRead":
        return cls(
            resource=usage.resource,
            segments=usage.segments,
            total_bytes=usage.total_bytes,
        )


class UserRead(BaseModel):
    """The caller's own account. Never exposes another user."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    email: EmailStr
    display_name: str | None
    created_at: datetime

    @classmethod
    def from_model(cls, user: User) -> "UserRead":
        return cls(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            created_at=user.created_at,
        )


class MeRead(BaseModel):
    """`GET /v1/me`: who the token belongs to, and what they have stored.

    ``usage`` holds one entry per resource the caller has captured, ordered by
    resource name; a resource never captured has no entry.
    """

    model_config = ConfigDict(frozen=True)

    user: UserRead
    usage: tuple[ResourceUsageRead, ...]
