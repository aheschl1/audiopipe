"""Every ``Depends`` provider, exported as ``Annotated`` aliases.

Routes declare ``user: CurrentUser`` to require authentication, and
``session: OwnedSession`` / ``segment: OwnedSegment`` to receive a row that is
already loaded and confirmed to belong to the caller.
"""

from typing import Annotated, AsyncIterator
from uuid import UUID

from fastapi import Depends, HTTPException, Path, Request, status
from fastapi.security import APIKeyCookie, HTTPAuthorizationCredentials, HTTPBearer

from api.schema.common import PageParams, RangeParams
from database.pipe import DatabasePipe
from database.schema.artifacts import PipelineArtifact
from database.schema.segments import ResourceSegment
from database.schema.sessions import RecordingSession
from database.schema.speakers import Speaker
from database.schema.users import User

# The web app's sign-in cookie, set by POST /v1/auth/session.
SESSION_COOKIE = "context_session"
# Cookie-authenticated writes must carry this header. A cross-site page can
# make the browser send the cookie, but not a custom header without a CORS
# preflight, which this API never grants.
CSRF_HEADER = "X-Requested-With"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# auto_error=False: missing credentials reach _authenticate, which raises 401.
_bearer = HTTPBearer(auto_error=False, description="An API token from `tokens issue`.")
_cookie = APIKeyCookie(
    name=SESSION_COOKIE, auto_error=False, description="Web sign-in, set by POST /v1/auth/session."
)

_UNAUTHENTICATED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="a valid bearer token is required",
    headers={"WWW-Authenticate": "Bearer"},
)
_CSRF = HTTPException(
    status_code=status.HTTP_403_FORBIDDEN,
    detail=f"cookie-authenticated writes need the {CSRF_HEADER} header",
)

_Bearer = Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]
_Cookie = Annotated[str | None, Depends(_cookie)]


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


async def _authenticate(
    request: Request, credentials: HTTPAuthorizationCredentials | None, cookie: str | None
) -> User:
    """The caller, from the Authorization header, else the sign-in cookie.

    Never from the URL: tokens in query strings land in logs and history.
    """
    secret: str | None = None
    if credentials is not None:
        secret = credentials.credentials
    elif cookie:
        if request.method not in _SAFE_METHODS and not request.headers.get(CSRF_HEADER):
            raise _CSRF
        secret = cookie
    user = await authenticate_bearer(secret)
    if user is None:
        raise _UNAUTHENTICATED
    return user


async def get_current_user(request: Request, credentials: _Bearer, cookie: _Cookie) -> User:
    """Resolve the bearer token or sign-in cookie to its owner, or raise 401."""
    return await _authenticate(request, credentials, cookie)


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


async def get_owned_artifact(
    user: CurrentUser,
    artifact_id: Annotated[UUID, Path(description="An artifact of one of your sessions.")],
) -> PipelineArtifact:
    """An artifact of a session the caller owns; else 404."""
    async with DatabasePipe() as pipe:
        artifact = await pipe.artifacts.get(artifact_id)
        session = (
            await pipe.sessions.get(artifact.session_id)
            if artifact is not None and artifact.session_id is not None
            else None
        )
    if artifact is None or session is None or session.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="artifact not found")
    return artifact


OwnedArtifact = Annotated[PipelineArtifact, Depends(get_owned_artifact)]


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
