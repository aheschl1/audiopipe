"""Pydantic models for the ``users`` table."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, SecretStr, field_validator


def normalize_email(value: str) -> str:
    return value.strip().lower()


class User(BaseModel):
    """A user as exposed to callers. Carries no password hash."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    email: EmailStr
    display_name: str | None = None
    is_active: bool
    # Signs in and reads, but every write is refused (the public demo account).
    read_only: bool = False
    created_at: datetime
    updated_at: datetime


class UserWithSecret(User):
    """Internal variant used during authentication only."""

    password_hash: str


class UserCreate(BaseModel):
    """Input for creating a user."""

    model_config = ConfigDict(frozen=True)

    email: EmailStr
    password: SecretStr
    display_name: str | None = None

    @field_validator("email", mode="before")
    @classmethod
    def _normalize_email(cls, value: object) -> object:
        return normalize_email(value) if isinstance(value, str) else value
