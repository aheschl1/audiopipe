"""Shared shapes for the tool modules."""

from uuid import UUID

from mcp.server.mcpserver.exceptions import ToolError

from api.mcp.timefmt import iso
from database.pipe import DatabasePipe
from database.schema.sessions import RecordingSession


def parse_uuid(value: str, field: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise ToolError(f"{field} must be a UUID") from None


def session_dict(session: RecordingSession) -> dict:
    duration_ms = None
    if session.ended_at is not None:
        duration_ms = int(
            (session.ended_at - session.started_at).total_seconds() * 1000
        )
    return {
        "session_id": str(session.id),
        "label": session.label,
        "device": session.device,
        "started_at": iso(session.started_at),
        "ended_at": iso(session.ended_at),  # null: still recording
        "duration_ms": duration_ms,
    }


async def owned_session(
    pipe: DatabasePipe, user_id: UUID, session_id: str
) -> RecordingSession:
    """Load a session the caller owns; another user's is "not found", same
    as the HTTP routes."""
    session = await pipe.sessions.get(parse_uuid(session_id, "session_id"))
    if session is None or session.user_id != user_id:
        raise ToolError("recording session not found")
    return session
