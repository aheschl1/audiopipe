"""Cross-session search tools: transcripts, sound tags, described audio."""

from fastapi import HTTPException

from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError

from api.embed_query import embed_query
from api.mcp.auth import require_user
from api.mcp.server import mcp
from api.mcp.timefmt import iso, parse_when
from api.mcp.tools.common import parse_uuid
from database.pipe import DatabasePipe
from database.repos.embeddings import AudioSearchHit
from database.repos.transcripts import TranscriptHit
from database.schema.tags import TagWindowHit


def _transcript_hit(hit: TranscriptHit, *, fuzzy: bool) -> dict:
    result = {
        "session_id": str(hit.session_id),
        "session_label": hit.session_label,
        "time": iso(hit.occurred_at),
        "start_ms": hit.start_ms,
        "end_ms": hit.end_ms,
        "speaker": hit.speaker_name or hit.metadata.get("speaker"),
        "text": hit.text,
        "score": round(hit.score, 4),
    }
    if not fuzzy:
        result["snippet"] = hit.snippet  # [[..]] marks the matched terms
    return result


@mcp.tool()
async def search_transcripts(
    query: str,
    ctx: Context,
    start: str | None = None,
    end: str | None = None,
    speaker: str | None = None,
    session_id: str | None = None,
    limit: int = 20,
) -> dict:
    """Find moments by what was said, across all sessions.

    Google-ish syntax: words AND together, `or`, `-exclude`, "quoted
    phrases" must appear adjacent. Falls back to trigram close-spelling
    matches (fuzzy=true) when strict matching finds nothing — misheard ASR
    words still land. start/end bound the wall clock (ISO-8601, naive =
    UTC); speaker filters to a named speaker (see list_speakers). Scores
    are comparable within one response only.
    """
    user = await require_user(ctx)
    since, until = parse_when(start, "start"), parse_when(end, "end")
    scope = parse_uuid(session_id, "session_id") if session_id else None
    async with DatabasePipe() as pipe:
        hits = await pipe.transcripts.search(
            user_id=user.id, q=query, session_id=scope,
            since=since, until=until, speaker=speaker, limit=limit,
        )
        fuzzy = not hits
        if fuzzy:
            hits = await pipe.transcripts.search_fuzzy(
                user_id=user.id, q=query, session_id=scope,
                since=since, until=until, speaker=speaker, limit=limit,
            )
    return {
        "query": query,
        "fuzzy": fuzzy and bool(hits),
        "hits": [_transcript_hit(hit, fuzzy=fuzzy) for hit in hits],
    }


def _audio_hit(hit: AudioSearchHit) -> dict:
    return {
        "session_id": str(hit.session_id),
        "session_label": hit.session_label,
        "time": iso(hit.occurred_at),
        "start_ms": hit.start_ms,
        "end_ms": hit.end_ms,
        "distance": round(hit.distance, 4),
        "top_labels": hit.metadata.get("labels", [])[:3],
    }


@mcp.tool()
async def search_audio(
    query: str,
    ctx: Context,
    start: str | None = None,
    end: str | None = None,
    session_id: str | None = None,
    limit: int = 20,
) -> dict:
    """Find ~10s audio windows by describing their sound in free text,
    e.g. 'keyboard typing' or 'a dog barking'.

    Contrastive (CLAP) ranking: distances are only comparable within one
    query, there is no absolute 'good match' cutoff — treat the order as
    the signal and check each hit's top_labels. For exact classes with
    thresholdable scores use search_sound_tags instead. start/end bound
    the wall clock (ISO-8601, naive = UTC).
    """
    user = await require_user(ctx)
    since, until = parse_when(start, "start"), parse_when(end, "end")
    scope = parse_uuid(session_id, "session_id") if session_id else None
    try:
        vector, model = await embed_query(query)
    except HTTPException:
        raise ToolError(
            "audio search is unavailable: the embedding sidecar is down "
            "(it may take tens of seconds to reload after idling — retry)"
        ) from None
    async with DatabasePipe() as pipe:
        hits = await pipe.audio_embeddings.search(
            vector, user_id=user.id, session_id=scope,
            since=since, until=until, limit=limit,
        )
    return {"query": query, "model": model, "hits": [_audio_hit(hit) for hit in hits]}


def _tag_hit(hit: TagWindowHit) -> dict:
    return {
        "session_id": str(hit.session_id),
        "session_label": hit.session_label,
        "time": iso(hit.occurred_at),
        "start_ms": hit.start_ms,
        "end_ms": hit.end_ms,
        "label": hit.label,
        "score": round(hit.score, 4),
    }


@mcp.tool()
async def search_sound_tags(
    label: str,
    ctx: Context,
    min_score: float = 0.3,
    start: str | None = None,
    end: str | None = None,
    session_id: str | None = None,
    limit: int = 20,
) -> dict:
    """Find ~10s audio windows where a sound class scored at least min_score.

    Deterministic complement to search_audio: exact AudioSet classes with
    calibrated sigmoid scores, comparable across queries. label matches
    case-insensitively as a substring — 'speech' finds 'Male speech, man
    speaking'; list_sound_labels gives the vocabulary. start/end bound the
    wall clock (ISO-8601, naive = UTC).
    """
    user = await require_user(ctx)
    since, until = parse_when(start, "start"), parse_when(end, "end")
    scope = parse_uuid(session_id, "session_id") if session_id else None
    async with DatabasePipe() as pipe:
        hits = await pipe.tags.search(
            user_id=user.id, label=label, min_score=min_score,
            session_id=scope, since=since, until=until, limit=limit,
        )
    return {"label": label, "min_score": min_score, "hits": [_tag_hit(h) for h in hits]}


@mcp.tool()
async def list_sound_labels(
    ctx: Context, start: str | None = None, end: str | None = None
) -> dict:
    """The sound classes heard in your audio, most frequent first — the
    vocabulary search_sound_tags filters on.

    start/end bound at session granularity (ISO-8601, naive = UTC); windows
    counts classified ~10s windows, best is the class's highest score.
    """
    user = await require_user(ctx)
    since, until = parse_when(start, "start"), parse_when(end, "end")
    async with DatabasePipe() as pipe:
        rows = await pipe.tags.labels(user_id=user.id, since=since, until=until)
    return {
        "labels": [
            {"label": row.label, "windows": row.windows, "best": round(row.best, 4)}
            for row in rows
        ]
    }
