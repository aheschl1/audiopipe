"""Request and response models for credential login."""

from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr


class LoginRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    email: EmailStr
    password: SecretStr
    name: str | None = Field(
        default="login", description="Label for the issued token, e.g. the device or app name."
    )


class WebSessionRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    email: EmailStr
    password: SecretStr


class TokenIssued(BaseModel):
    """The plaintext exists exactly once, here; the server stores only a hash."""

    model_config = ConfigDict(frozen=True)

    token: str = Field(description="Bearer token for the Authorization header.")
