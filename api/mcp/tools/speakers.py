"""Speaker tools: who exists, and everything one person said."""

from mcp.server.mcpserver.context import Context

from api.mcp.auth import require_user
from api.mcp.server import mcp
from api.mcp.timefmt import iso
from database.pipe import DatabasePipe


@mcp.tool()
async def list_speakers(ctx: Context, limit: int = 50, offset: int = 0) -> dict:
    """The clustered voices across all sessions, with membership counts.

    name is the user-given label (null: nobody has named the cluster yet);
    names are the values search_transcripts' speaker filter and
    get_speaker_transcripts accept. Names are deliberately non-unique — an
    imperfect split can leave two clusters of one person sharing a name.
    """
    user = await require_user(ctx)
    async with DatabasePipe() as pipe:
        rows = await pipe.speakers.list_for_user(user.id, limit=limit, offset=offset)
    return {
        "speakers": [
            {
                "speaker_id": str(row.id),
                "name": row.name,
                "voiceprints": row.embeddings,
                "sessions": row.sessions,
            }
            for row in rows
        ]
    }


@mcp.tool()
async def get_speaker_transcripts(
    name: str, ctx: Context, limit: int = 50, offset: int = 0
) -> dict:
    """Everything said by every cluster sharing this name, newest session
    first.

    Each utterance carries its absolute wall-clock ``time`` plus the
    session-relative span; page with limit/offset.
    """
    user = await require_user(ctx)
    async with DatabasePipe() as pipe:
        rows = await pipe.speakers.transcripts_for_name(
            user.id, name, limit=limit + 1, offset=offset
        )
    return {
        "name": name,
        "utterances": [
            {
                "session_id": str(row.session_id),
                "time": iso(row.occurred_at),
                "start_ms": row.start_ms,
                "end_ms": row.end_ms,
                "text": row.text,
            }
            for row in rows[:limit]
        ],
        "next_offset": offset + limit if len(rows) > limit else None,
    }
