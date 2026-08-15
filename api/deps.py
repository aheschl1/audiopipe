"""Every ``Depends`` provider, exported as ``Annotated`` aliases.

Routes declare ``user: CurrentUser`` to require authentication, and
``session: OwnedSession`` / ``segment: OwnedSegment`` to receive a row that is
already loaded and confirmed to belong to the caller.
"""

from typing import Annotated, AsyncIterator
from uuid import UUID

from fastapi import Depends, HTTPException, Path, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from api.schema.common import PageParams, RangeParams
from database.pipe import DatabasePipe
from database.schema.segments import ResourceSegment
from database.schema.sessions import RecordingSession
from database.schema.speakers import Speaker
from database.schema.users import User

# auto_error=False: a missing header reaches get_current_user, which raises 401.
_bearer = HTTPBearer(auto_error=False, description="An API token from `tokens issue`.")

_UNAUTHENTICATED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="a valid bearer token is required",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_pipe() -> AsyncIterator[DatabasePipe]:
    """One connection and one transaction for the whole request.

    FastAPI caches dependencies per request, so the auth dependency and the
    route body share this pipe. The transaction commits when the request ends.
    """
    async with DatabasePipe() as pipe:
        yield pipe


Pipe = Annotated[DatabasePipe, Depends(get_pipe)]


async def authenticate_token(pipe: DatabasePipe, token: str | None) -> User | None:
    """Resolve a bearer token to its owner; None when missing or invalid.

    ``tokens.authenticate`` rejects unknown, revoked and expired tokens and
    inactive users, and stamps ``last_used_at`` in the same statement — the
    stamp is throttled, so the common case is one read-only round trip. Shared
    with the websocket upgrade check, which cannot use
    ``HTTPBearer``/``HTTPException``.
    """
    if not token:
        return None
    return await pipe.tokens.authenticate(token)


async def authenticate_bearer(token: str | None) -> User | None:
    """Authenticate on a dedicated, immediately-committed transaction.

    Auth deliberately does not ride on the request's ``get_pipe`` transaction:
    that one stays open until the response finishes -- for a streaming audio
    response, until the last byte is sent -- and holding the ``last_used_at``
    row lock (and a pooled connection) for that long serializes every other
    request from the same token. Here the lock and connection are released the
    moment auth returns.
    """
    async with DatabasePipe() as pipe:
        return await authenticate_token(pipe, token)


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    """Resolve the bearer token to its owner, or raise 401."""
    user = await authenticate_bearer(credentials.credentials if credentials else None)
    if user is None:
        raise _UNAUTHENTICATED
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def _load_owned_session(user: User, session_id: UUID) -> RecordingSession:
    """Load a session the caller owns. Another user's session raises 404.

    Resolved on its own short-lived connection rather than the request's
    ``get_pipe``: the returned model is detached, so streaming routes that only
    need the row hold no pooled connection while their response is sent.
    """
    async with DatabasePipe() as pipe:
        session = await pipe.sessions.get(session_id)
    if session is None or session.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="recording session not found"
        )
    return session


async def get_owned_session(
    user: CurrentUser,
    session_id: Annotated[UUID, Path(description="A recording session belonging to you.")],
) -> RecordingSession:
    return await _load_owned_session(user, session_id)


OwnedSession = Annotated[RecordingSession, Depends(get_owned_session)]


async def get_playable_session(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session_id: Annotated[UUID, Path(description="A recording session belonging to you.")],
    token: Annotated[
        str | None,
        Query(
            description="Bearer token as a query parameter, for media elements "
            "that cannot send an Authorization header. Mint a short-lived one "
            "via POST /v1/sessions/{session_id}/playback. Ignored when the "
            "header is present."
        ),
    ] = None,
) -> RecordingSession:
    """``OwnedSession`` for media routes: `<audio src>` cannot set headers, so
    the token may arrive as ``?token=``. Any live token works — the query
    channel widens *transport*, not privilege — but URLs land in logs and
    history, which is why the playback route mints minutes-lived ones."""
    user = await authenticate_bearer(
        credentials.credentials if credentials else token
    )
    if user is None:
        raise _UNAUTHENTICATED
    return await _load_owned_session(user, session_id)


PlayableSession = Annotated[RecordingSession, Depends(get_playable_session)]


async def get_owned_segment(
    user: CurrentUser,
    segment_id: Annotated[UUID, Path(description="A segment belonging to you.")],
) -> ResourceSegment:
    """Load a segment the caller owns. Another user's segment raises 404.

    On its own short-lived connection (see :func:`get_owned_session`): the
    audio-download route holds no pooled connection while it streams.
    """
    async with DatabasePipe() as pipe:
        segment = await pipe.segments.get(segment_id)
    if segment is None or segment.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="segment not found"
        )
    return segment


OwnedSegment = Annotated[ResourceSegment, Depends(get_owned_segment)]


async def get_owned_speaker(
    user: CurrentUser,
    speaker_id: Annotated[UUID, Path(description="A speaker cluster belonging to you.")],
) -> Speaker:
    """Load a speaker the caller owns. Another user's speaker raises 404."""
    async with DatabasePipe() as pipe:
        speaker = await pipe.speakers.get(speaker_id)
    if speaker is None or speaker.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="speaker not found"
        )
    return speaker


OwnedSpeaker = Annotated[Speaker, Depends(get_owned_speaker)]

Paging = Annotated[PageParams, Depends()]
Range = Annotated[RangeParams, Depends()]
