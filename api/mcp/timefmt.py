"""Wall-clock helpers for the MCP tools.

Tools speak ISO-8601 both ways; artifacts are stored as ms offsets from
their session's ``started_at``, so results project both forms.
"""

from datetime import UTC, datetime, timedelta

from mcp.server.mcpserver.exceptions import ToolError


def parse_when(value: str | None, field: str) -> datetime | None:
    """An ISO-8601 timestamp as an aware datetime; naive values are UTC."""
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ToolError(
            f"{field} must be an ISO-8601 timestamp, e.g. 2026-08-17T09:00:00-06:00"
        ) from None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def at(started_at: datetime, offset_ms: int | None) -> str | None:
    """The wall-clock ISO moment ``offset_ms`` into a session."""
    if offset_ms is None:
        return None
    return (started_at + timedelta(milliseconds=offset_ms)).isoformat()


def iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.isoformat()
