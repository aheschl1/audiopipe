"""Recording sessions: create, list, inspect, end, and per-resource media.

Resources enter a session through the streaming websocket (see
``api.routes.stream``), not through these routes.
"""

import resources as resource_registry

from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse

from api import session_audio, timeline_events
from services import stitch
from api.deps import CurrentUser, OwnedSession, Paging, Pipe, PlayableSession, Range
from api.ranges import RangeNotSatisfiable, etag_matches, parse_range
from api.schema.artifacts import ArtifactRead
from api.schema.auth import PlaybackTokenRead
from api.schema.common import ErrorResponse, Page
from api.schema.locations import LocationPointRead
from api.schema.segments import SegmentRead, SessionResourceRead, segment_read
from api.schema.sessions import (
    SessionCreateRequest,
    SessionLabelRequest,
    SessionRead,
    SplitAllResponse,
    TranscriptEditRequest,
)
from api.schema.speakers import SessionSpeakerRead
from api.schema.timeline import TimelineEvent
from database.schema.sessions import SessionCreate
from storage.pipe import BlobPipe

router = APIRouter(prefix="/sessions", tags=["sessions"])

# Far past any real session's artifact count; the timeline pages over
# *events* (one artifact yields several), so the rows cannot be paged in SQL.
_MAX_TIMELINE_ARTIFACTS = 1_000_000


@router.post("", status_code=status.HTTP_201_CREATED, summary="Start a recording session")
async def create_session(
    user: CurrentUser, pipe: Pipe, body: SessionCreateRequest
) -> SessionRead:
    """Create an open session, then attach to it over the streaming websocket."""
    session = await pipe.sessions.create(
        SessionCreate(
            user_id=user.id,
            device=body.device,
            label=body.label,
            started_at=body.started_at,
            metadata=body.metadata,
        )
    )
    return SessionRead.from_model(session)


@router.get("", summary="List your recording sessions")
async def list_sessions(
    user: CurrentUser,
    pipe: Pipe,
    paging: Paging,
    open_only: bool = Query(False, description="Only sessions that have not ended."),
) -> Page[SessionRead]:
    """Newest first, scoped to the caller."""
    rows = await pipe.sessions.list_for_user(
        user.id, open_only=open_only, limit=paging.probe_limit, offset=paging.offset
    )
    return Page.build(rows, paging, SessionRead.from_model)


# Declared before the /{session_id} routes: "split" is not a UUID, but keeping
# the static segment first spares the router the 422-vs-404 ambiguity.
@router.post("/split", summary="Split every open session")
async def split_all_sessions(user: CurrentUser, pipe: Pipe) -> SplitAllResponse:
    """End all of your open sessions as rotations: processing starts on each,
    and any connected client is told to reopen and keep recording."""
    return SplitAllResponse(split=await pipe.sessions.split_all_open(user.id))


@router.get("/{session_id}", summary="Fetch one recording session")
async def get_session(session: OwnedSession) -> SessionRead:
    return SessionRead.from_model(session)


@router.post("/{session_id}/split", summary="Split a recording session")
async def split_session(session: OwnedSession, pipe: Pipe) -> SessionRead:
    """End the session so processing starts, telling a connected client to
    open a successor. Unlike ``end``, this signals "keep recording"; splitting
    an already-ended session returns it unchanged."""
    if not session.is_open:
        return SessionRead.from_model(session)
    return SessionRead.from_model(await pipe.sessions.split(session.id))


@router.post("/{session_id}/label", summary="Rename (or unname) a session")
async def label_session(
    session: OwnedSession, pipe: Pipe, body: SessionLabelRequest
) -> SessionRead:
    """Set the session's display name, or clear it with an explicit null.
    Purely cosmetic — nothing downstream keys off the label."""
    return SessionRead.from_model(await pipe.sessions.set_label(session.id, body.label))


@router.post("/{session_id}/end", summary="End a recording session")
async def end_session(session: OwnedSession, pipe: Pipe) -> SessionRead:
    """Idempotent in effect; re-ending only moves the end timestamp."""
    return SessionRead.from_model(await pipe.sessions.end(session.id))


# Minutes, not hours: the token rides in the audio URL's query string, where
# it lands in server logs and browser history. Short life bounds that leak.
_PLAYBACK_TTL = timedelta(minutes=15)
_PLAYBACK_TOKEN_NAME = "playback"


@router.post("/{session_id}/playback", summary="Mint a short-lived media playback token")
async def create_playback_token(session: OwnedSession, pipe: Pipe) -> PlaybackTokenRead:
    """A token for the media route's ``?token=`` parameter.

    Media elements (``<audio src>``) cannot send an Authorization header, so
    the session-media route also accepts a token in the URL. This mints one
    whose lifetime is minutes; the player re-mints when it expires. It is a
    real API token, just time-boxed — expired ``playback`` rows are purged
    opportunistically on each mint.
    """
    await pipe.tokens.purge_expired_named(session.user_id, _PLAYBACK_TOKEN_NAME)
    issued = await pipe.tokens.issue(
        session.user_id, name=_PLAYBACK_TOKEN_NAME, ttl=_PLAYBACK_TTL
    )
    assert issued.record.expires_at is not None  # ttl was given
    return PlaybackTokenRead(
        token=issued.token.get_secret_value(), expires_at=issued.record.expires_at
    )


@router.get("/{session_id}/segments", summary="List a session's segments")
async def list_session_segments(
    session: OwnedSession,
    pipe: Pipe,
    paging: Paging,
    resource: str | None = Query(None, description="Only segments of this resource."),
) -> Page[SegmentRead]:
    """In capture order (by `sequence`), interleaved across resources."""
    rows = await pipe.segments.list_for_session(
        session.id, resource=resource, limit=paging.probe_limit, offset=paging.offset
    )
    return Page.build(rows, paging, segment_read)


@router.get("/{session_id}/resources", summary="What this session holds, per resource")
async def list_session_resources(
    session: OwnedSession, pipe: Pipe
) -> list[SessionResourceRead]:
    """One entry per resource with any segments, ordered by resource name."""
    rows = await pipe.segments.resource_summary(session.id)
    return [SessionResourceRead.from_model(row) for row in rows]


@router.get(
    "/{session_id}/resources/location/points",
    summary="A session's location points by timeline position",
)
async def list_session_location_points(
    session: OwnedSession,
    pipe: Pipe,
    paging: Paging,
    window: Range,
) -> Page[LocationPointRead]:
    """The session's fixes in timeline order, windowed like the timeline —
    adjacent windows partition the stream. A session with no location
    resource yields an empty page, like an unprocessed session's timeline
    serves just its frames.
    """
    rows = await pipe.locations.points_for_session(
        session.id,
        from_ms=window.from_ms,
        to_ms=window.to_ms,
        limit=paging.probe_limit,
        offset=paging.offset,
    )
    return Page.build(rows, paging, LocationPointRead.from_model)


@router.get("/{session_id}/artifacts", summary="List a session's pipeline artifacts")
async def list_session_artifacts(
    session: OwnedSession,
    pipe: Pipe,
    paging: Paging,
    window: Range,
    pipeline: str | None = Query(None, description="Only artifacts of this pipeline."),
    kind: str | None = Query(None, description="Only artifacts of this kind."),
) -> Page[ArtifactRead]:
    """What the processing tiers attached to this session, in timeline order.

    Whole-session artifacts sort first; a `from_ms`/`to_ms` window restricts
    to span artifacts overlapping it.
    """
    rows = await pipe.artifacts.list_for_session(
        session.id,
        pipeline=pipeline,
        kind=kind,
        from_ms=window.from_ms,
        to_ms=window.to_ms,
        limit=paging.probe_limit,
        offset=paging.offset,
    )
    return Page.build(rows, paging, ArtifactRead.from_model)


@router.get("/{session_id}/timeline", summary="A session's ordered event timeline")
async def get_session_timeline(
    session: OwnedSession,
    pipe: Pipe,
    paging: Paging,
    window: Range,
) -> Page[TimelineEvent]:
    """What happened when: session frames, speech starts/ends, transcripts,
    sound tags, the sound spans built from them, and location points.

    Events carry a ``type`` discriminator; clients must ignore types they do
    not recognise — future processing tiers add new ones. `limit`/`offset`
    page over events (one artifact can yield several, so offsets here do not
    line up with the artifacts route). A `from_ms`/`to_ms` window keeps only
    events positioned inside it, so adjacent windows partition the stream —
    a span straddling a boundary contributes its `speech-end` alone, and a
    `sound-span` that began before the window is left out entirely.

    An unprocessed (or speechless) session serves just its session frames;
    processing status stays introspectable via `.../artifacts?kind=speech-map`.
    Location points come straight from the stored segments — no pipeline —
    so they appear live while the session is still streaming.
    """
    rows = await pipe.artifacts.list_for_session(
        session.id, from_ms=window.from_ms, to_ms=window.to_ms, limit=_MAX_TIMELINE_ARTIFACTS
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
        session,
        rows,
        segments=segments,
        from_ms=window.from_ms,
        to_ms=window.to_ms,
        speakers=speaker_map,
    )
    sliced = events[paging.offset : paging.offset + paging.probe_limit]
    return Page.build(sliced, paging, lambda event: event)


@router.post("/{session_id}/transcripts/{artifact_id}", summary="Edit a transcript's text")
async def edit_transcript(
    session: OwnedSession, artifact_id: UUID, pipe: Pipe, body: TranscriptEditRequest
) -> ArtifactRead:
    """Rewrite one utterance's text. The edit lands in the artifact's
    metadata (text, chars, and an ``edited`` provenance flag), so the
    timeline and full-text search serve it immediately — both read
    ``metadata->>'text'`` live."""
    artifact = await pipe.artifacts.get(artifact_id)
    if (
        artifact is None
        or artifact.session_id != session.id
        or (artifact.pipeline, artifact.kind) != ("transcribe", "transcript")
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="transcript not found")
    updated = await pipe.artifacts.merge_metadata(
        artifact_id,
        {"text": body.text, "chars": len(body.text), "edited": True},
        drop=("words",),  # stale word timings are worse than none
    )
    assert updated is not None  # just fetched it, same transaction
    return ArtifactRead.from_model(updated)


@router.get("/{session_id}/speakers", summary="Who spoke in this session")
async def list_session_speakers(
    session: OwnedSession, pipe: Pipe
) -> list[SessionSpeakerRead]:
    """One row per voice: clustered voices grouped under their global
    speaker (labeled or not), with each not-yet-clustered block-local label
    on its own row — nothing the diarizer heard is hidden.
    """
    labels = await pipe.speakers.labels_for_session(session.id)
    grouped: dict = {}
    for label in labels:
        # Unassigned labels get their own rows: two unknown voices must not
        # be lumped into one "unclustered" bucket.
        key = label.speaker_id or f"local:{label.speaker}"
        entry = grouped.setdefault(
            key, {"speaker_id": label.speaker_id, "name": label.name,
                  "local_labels": [], "talk_ms": 0}
        )
        entry["local_labels"].append(label.speaker)
        entry["talk_ms"] += label.talk_ms or 0
    return [
        SessionSpeakerRead(**entry)
        for entry in sorted(grouped.values(), key=lambda e: -e["talk_ms"])
    ]


async def _stream_stitched(
    header: bytes, plan: stitch.StitchPlan, start: int, end: int
) -> AsyncIterator[bytes]:
    """Yield the stitched range: header bytes, then blob ranges in order.

    Mirrors ``segments._stream_audio``: the blob store opens inside the
    generator so FastAPI's teardown cannot outrun the body, and the first
    chunk is pulled eagerly so a missing object still surfaces as a 404.
    """

    async def chunks() -> AsyncIterator[bytes]:
        async with BlobPipe() as blobs:
            for piece, piece_start, piece_end in stitch.slices(plan, start, end):
                if piece is None:
                    yield header[piece_start : piece_end + 1]
                    continue
                async for chunk in blobs.stream(
                    piece.object_key, start=piece_start, end=piece_end
                ):
                    yield chunk

    stream = chunks()
    try:
        first = await anext(stream)
    except StopAsyncIteration:
        first = b""

    async def body() -> AsyncIterator[bytes]:
        yield first
        async for chunk in stream:
            yield chunk

    return body()


@router.get(
    "/{session_id}/resources/{resource}/media",
    summary="Stream a session's rendered media for one resource",
    response_class=StreamingResponse,
    responses={
        200: {"content": {"audio/wav": {}}, "description": "The whole session, rendered."},
        206: {"content": {"audio/wav": {}}, "description": "The requested byte range."},
        304: {"description": "The client's cached copy is still current."},
        404: {
            "description": "Unknown or non-renderable resource, or the session holds none of it."
        },
        409: {
            "model": ErrorResponse,
            "description": "The session's segments do not form one continuous stream.",
        },
        416: {"description": "The requested range lies outside the media."},
    },
)
async def get_session_media(
    session: PlayableSession, resource: str, request: Request
) -> Response:
    """The session's whole stream of one resource as a single media object.

    Only resources whose type is *renderable* serve media; today that is
    audio — every segment's PCM behind a single WAV header, in sequence
    order. The stitched size is known from the rows alone, so `Range`
    (seeking) and conditional requests work exactly as on the per-segment
    route. An open session serves what has been ingested so far; re-request
    for more.

    The representation (header, plan, ETag) is cached per session and reused
    until its segments change, so a conditional check or a range seek costs one
    cheap fingerprint query rather than a full re-read. No pooled database
    connection is held while the body streams from the blob store.
    """
    try:
        rtype = resource_registry.get(resource)
    except KeyError:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content=ErrorResponse(detail=f"unknown resource {resource!r}").model_dump(),
        )
    if not rtype.renderable:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content=ErrorResponse(
                detail=f"resource {resource!r} has no rendered media"
            ).model_dump(),
        )
    # Audio is the only renderable resource; a second one would dispatch on
    # rtype here instead of falling through to the stitched-WAV path.
    try:
        audio = await session_audio.resolve_session_audio(session)
    except stitch.NotStitchable as exc:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=ErrorResponse(detail=str(exc)).model_dump(),
        )
    if audio is None:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content=ErrorResponse(detail="this session holds no audio yet").model_dump(),
        )

    etag = audio.etag
    plan = audio.plan
    header = audio.header
    headers = {
        "ETag": etag,
        "Accept-Ranges": "bytes",
        # A closed session's audio can never grow; an open one grows per chunk.
        "Cache-Control": (
            "private, no-cache" if session.is_open else "private, max-age=31536000, immutable"
        ),
        "Content-Disposition": f'inline; filename="session-{session.id}.wav"',
    }

    if etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)

    range_header = request.headers.get("range")
    if_range = request.headers.get("if-range")
    if if_range and not etag_matches(if_range, etag):
        range_header = None

    try:
        byte_range = parse_range(range_header, plan.total_size)
    except RangeNotSatisfiable:
        return Response(
            status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
            headers={**headers, "Content-Range": f"bytes */{plan.total_size}"},
        )

    if byte_range is None:
        return StreamingResponse(
            await _stream_stitched(header, plan, 0, plan.total_size - 1),
            media_type="audio/wav",
            headers={**headers, "Content-Length": str(plan.total_size)},
        )

    return StreamingResponse(
        await _stream_stitched(header, plan, byte_range.start, byte_range.end),
        status_code=status.HTTP_206_PARTIAL_CONTENT,
        media_type="audio/wav",
        headers={
            **headers,
            "Content-Length": str(byte_range.length),
            "Content-Range": byte_range.content_range(plan.total_size),
        },
    )
