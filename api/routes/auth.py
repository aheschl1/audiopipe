"""Sign-in: API tokens for devices and scripts, an HttpOnly cookie for the web app.

The cookie is what lets ``<img>``, ``<audio>`` and ``<video>`` load media: they
send cookies but can't send an Authorization header.
"""

from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request, Response, status

from api.config import get_settings
from api.deps import CSRF_HEADER, SESSION_COOKIE, Pipe
from api.schema.auth import LoginRequest, TokenIssued, WebSessionRequest
from api.schema.users import UserRead

router = APIRouter(prefix="/auth", tags=["auth"])


_INVALID = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid email or password")
_WEB_TOKEN_NAME = "web"


@router.post("/login", summary="Exchange email + password for an API token")
async def login(pipe: Pipe, body: LoginRequest) -> TokenIssued:
    """Verify the credentials and mint a token (equivalent to `tokens issue`).

    Unknown email and wrong password are indistinguishable in both response
    and timing, so account existence does not leak.
    """
    user = await pipe.users.authenticate(body.email, body.password.get_secret_value())
    if user is None:
        raise _INVALID
    issued = await pipe.tokens.issue(user.id, name=body.name)
    return TokenIssued(token=issued.token.get_secret_value())


@router.post("/session", summary="Sign the web app in with a cookie")
async def create_web_session(pipe: Pipe, body: WebSessionRequest, response: Response) -> UserRead:
    """Verify the credentials and set an HttpOnly ``context_session`` cookie.

    The cookie holds a token that expires after ``API_WEB_SESSION_DAYS``. It is
    sent on every same-site request, media elements included; writes made
    with it must also send the ``X-Requested-With`` header.
    """
    user = await pipe.users.authenticate(body.email, body.password.get_secret_value())
    if user is None:
        raise _INVALID
    settings = get_settings()
    ttl = timedelta(days=settings.web_session_days)
    await pipe.tokens.purge_expired_named(user.id, _WEB_TOKEN_NAME)
    issued = await pipe.tokens.issue(user.id, name=_WEB_TOKEN_NAME, ttl=ttl)
    response.set_cookie(
        SESSION_COOKIE,
        issued.token.get_secret_value(),
        max_age=int(ttl.total_seconds()),
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )
    return UserRead.from_model(user)


@router.delete("/session", status_code=status.HTTP_204_NO_CONTENT, summary="Sign the web app out")
async def delete_web_session(request: Request, pipe: Pipe) -> Response:
    """Revoke the cookie's token and clear the cookie. Idempotent."""
    if not request.headers.get(CSRF_HEADER):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"send the {CSRF_HEADER} header")
    secret = request.cookies.get(SESSION_COOKIE)
    if secret and (token := await pipe.tokens.resolve(secret)) is not None:
        await pipe.tokens.revoke(token.id)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(SESSION_COOKIE, path="/", secure=get_settings().cookie_secure, httponly=True, samesite="lax")
    return response
