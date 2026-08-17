"""Session tools: wall-clock discovery, transcripts, timeline, day summary."""

from datetime import timedelta

from psycopg.rows import dict_row

from mcp.server.mcpserver.context import Context

from api import timeline_events
from api.mcp.auth import require_user
from api.mcp.server import mcp
from api.mcp.timefmt import at, iso, parse_when
from api.mcp.tools.common import owned_session, session_dict
from database.pipe import DatabasePipe
from database.schema.sessions import RecordingSession

# Same ceiling as the HTTP timeline route: assembly is in-memory planning.
_MAX_TIMELINE_ARTIFACTS = 1_000_000

# Identifier/provenance fields an agent doesn't need per event.
_EVENT_DROP = {"artifact_id", "segment_id", "voiceprint_id", "speaker_id", "chars", "model"}


@mcp.tool()
async def list_sessions(
    start: str, end: str, ctx: Context, limit: int = 50, offset: int = 0
) -> dict:
    """Recording sessions overlapping the wall-clock window [start, end), oldest first.

    start/end are ISO-8601 timestamps; naive values are read as UTC. A null
    ended_at means the session is still recording. Page with limit/offset.
    """
    user = await require_user(ctx)
    async with DatabasePipe() as pipe:
        rows = await pipe.sessions.overlapping(
            user.id, parse_when(start, "start"), parse_when(end, "end"),
            limit=limit, offset=offset,
        )
    return {"sessions": [session_dict(row) for row in rows]}


@mcp.tool()
async def get_session(session_id: str, ctx: Context) -> dict:
    """One session's frame: times, who spoke, and what processing attached.

    Artifact counts are per (pipeline, kind); transcribe/transcript rows are
    the utterances get_transcript reads. Speakers list every diarized voice
    with its user-given name when the voice has been clustered and named.
    """
    user = await require_user(ctx)
    async with DatabasePipe() as pipe:
        session = await owned_session(pipe, user.id, session_id)
        labels = await pipe.speakers.labels_for_session(session.id)
        async with pipe.connection.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                    SELECT pipeline, kind, count(*) AS artifacts
                    FROM pipeline_artifacts WHERE session_id = %s
                    GROUP BY pipeline, kind ORDER BY pipeline, kind
                """,
                (session.id,),
            )
            counts = await cur.fetchall()
    return {
        **session_dict(session),
        "speakers": [
            {"label": row.speaker, "name": row.name, "talk_ms": row.talk_ms}
            for row in labels
        ],
        "artifacts": [dict(row) for row in counts],
    }


@mcp.tool()
async def get_transcript(
    session_id: str,
    ctx: Context,
    from_ms: int | None = None,
    to_ms: int | None = None,
    offset: int = 0,
    limit: int = 200,
) -> dict:
    """One session's transcript as speaker-attributed lines.

    Lines read ``[HH:MM:SS] name: text`` with absolute UTC times (the date
    is started_at's); voices without a named cluster keep their diarizer
    label. from_ms/to_ms window the session-relative range; a non-null
    next_offset means more utterances remain.
    """
    user = await require_user(ctx)
    async with DatabasePipe() as pipe:
        session = await owned_session(pipe, user.id, session_id)
        rows = await pipe.artifacts.list_for_session(
            session.id, pipeline="transcribe", kind="transcript",
            from_ms=from_ms, to_ms=to_ms, limit=limit + 1, offset=offset,
        )
        labels = await pipe.speakers.labels_for_session(session.id)
    names = {row.speaker: row.name for row in labels if row.name}
    lines = []
    for row in rows[:limit]:
        speaker = row.metadata.get("speaker")
        moment = session.started_at + timedelta(milliseconds=row.start_ms or 0)
        who = names.get(speaker) or speaker or "unknown"
        lines.append(f"[{moment:%H:%M:%S}] {who}: {row.metadata.get('text', '')}")
    return {
        "session_id": str(session.id),
        "label": session.label,
        "started_at": iso(session.started_at),
        "text": "\n".join(lines),
        "utterances": len(lines),
        "next_offset": offset + limit if len(rows) > limit else None,
    }


def _compact_event(event, session: RecordingSession) -> dict:
    data = event.model_dump(mode="json", exclude_none=True)
    for key in _EVENT_DROP & data.keys():
        del data[key]
    if data.get("type") == "audio-tag":
        data["labels"] = data.get("labels", [])[:5]
    data["time"] = at(session.started_at, event.at_ms)
    return data


@mcp.tool()
async def get_timeline(
    session_id: str,
    ctx: Context,
    from_ms: int | None = None,
    to_ms: int | None = None,
    limit: int = 200,
    offset: int = 0,
) -> dict:
    """What happened when in one session, as compact ordered events.

    Event types: session-start/end, speech-start/end, transcript, audio-tag
    (~10s classified windows), sound-span (one class holding over a
    stretch), location-point. Each carries ``time`` (absolute ISO) and
    ``at_ms`` (session-relative). from_ms/to_ms window the range; a non-null
    next_offset means more events remain.
    """
    user = await require_user(ctx)
    async with DatabasePipe() as pipe:
        session = await owned_session(pipe, user.id, session_id)
        rows = await pipe.artifacts.list_for_session(
            session.id, from_ms=from_ms, to_ms=to_ms, limit=_MAX_TIMELINE_ARTIFACTS
        )
        segments = [
            segment
            for resource in timeline_events.timeline_resources()
            for segment in await pipe.segments.list_for_session(
                session.id, resource=resource, limit=_MAX_TIMELINE_ARTIFACTS
            )
        ]
        labels = await pipe.speakers.labels_for_session(session.id)
    speaker_map = {
        label.speaker: label for label in labels if label.speaker_id is not None
    }
    events = timeline_events.assemble(
        session, rows, segments=segments, from_ms=from_ms, to_ms=to_ms,
        speakers=speaker_map,
    )
    window = events[offset : offset + limit]
    return {
        "session_id": str(session.id),
        "events": [_compact_event(event, session) for event in window],
        "total": len(events),
        "next_offset": offset + limit if offset + limit < len(events) else None,
    }


@mcp.tool()
async def day_summary(start: str, end: str, ctx: Context) -> dict:
    """An overview of a wall-clock window: sessions recorded, most frequent
    sound classes, and per-speaker talk time.

    The natural first call for journaling or "what happened today" — drill
    into the results with get_transcript, get_timeline and the search tools.
    """
    user = await require_user(ctx)
    since, until = parse_when(start, "start"), parse_when(end, "end")
    async with DatabasePipe() as pipe:
        sessions = await pipe.sessions.overlapping(user.id, since, until, limit=200)
        sounds = await pipe.tags.labels(user_id=user.id, since=since, until=until)
        talk = await pipe.speakers.talk_time(user.id, since=since, until=until)
    return {
        "sessions": [session_dict(row) for row in sessions],
        "top_sounds": [
            {"label": row.label, "windows": row.windows, "best": row.best}
            for row in sounds[:15]
        ],
        "talk_time": [
            {"speaker_id": str(row.speaker_id), "name": row.name, "talk_ms": row.talk_ms}
            for row in talk
        ],
    }
