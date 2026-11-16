"""One module per resource, each exposing a ``router``."""

from api.routes import (
    auth,
    automations,
    health,
    moments,
    resources,
    search,
    segments,
    sessions,
    speakers,
    stream,
    users,
)

__all__ = [
    "auth",
    "automations",
    "health",
    "moments",
    "resources",
    "search",
    "segments",
    "sessions",
    "speakers",
    "stream",
    "users",
]
