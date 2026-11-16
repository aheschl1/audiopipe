"""Token authentication: every way in, and every way refused."""

import asyncio
import time
from datetime import timedelta

import httpx

from database.pipe import DatabasePipe
from tests.e2e.conftest import Account

PROTECTED = "/v1/sessions"


async def test_valid_token_is_accepted(client: httpx.AsyncClient, account: Account) -> None:
    assert (await client.get(PROTECTED, headers=account.headers)).status_code == 200


async def test_missing_header_is_refused(client: httpx.AsyncClient, account: Account) -> None:
    response = await client.get(PROTECTED)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {"detail": "a valid bearer token is required"}


async def test_unknown_token_is_refused(client: httpx.AsyncClient, account: Account) -> None:
    response = await client.get(PROTECTED, headers={"Authorization": "Bearer not-a-real-token"})
    assert response.status_code == 401


async def test_other_auth_scheme_is_refused(client: httpx.AsyncClient, account: Account) -> None:
    response = await client.get(PROTECTED, headers={"Authorization": "Basic dXNlcjpwYXNz"})
    assert response.status_code == 401


async def test_bearer_without_credentials_is_refused(
    client: httpx.AsyncClient, account: Account
) -> None:
    response = await client.get(PROTECTED, headers={"Authorization": "Bearer"})
    assert response.status_code == 401


async def test_revoked_token_is_refused(client: httpx.AsyncClient, account: Account) -> None:
    async with DatabasePipe() as pipe:
        issued = await pipe.tokens.list_for_user(account.user.id)
        assert await pipe.tokens.revoke(issued[0].id) is True

    assert (await client.get(PROTECTED, headers=account.headers)).status_code == 401


async def test_expired_token_is_refused(client: httpx.AsyncClient, account: Account) -> None:
    async with DatabasePipe() as pipe:
        expired = await pipe.tokens.issue(account.user.id, ttl=timedelta(seconds=-1))

    headers = {"Authorization": f"Bearer {expired.token.get_secret_value()}"}
    assert (await client.get(PROTECTED, headers=headers)).status_code == 401


async def test_deactivated_user_cannot_use_a_live_token(
    client: httpx.AsyncClient, account: Account
) -> None:
    async with DatabasePipe() as pipe:
        await pipe.users.set_active(account.user.id, False)

    assert (await client.get(PROTECTED, headers=account.headers)).status_code == 401


async def test_login_issues_a_working_token(
    client: httpx.AsyncClient, account: Account
) -> None:
    """POST /v1/auth/login with the seeded password mints a usable token."""
    response = await client.post(
        "/v1/auth/login",
        json={"email": account.user.email, "password": "s3cret", "name": "e2e-login"},
    )
    assert response.status_code == 200
    token = response.json()["token"]
    assert token != account.token  # a fresh token, not the seeded one

    me = await client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["user"]["email"] == account.user.email

    async with DatabasePipe() as pipe:
        names = {t.name for t in await pipe.tokens.list_for_user(account.user.id)}
    assert "e2e-login" in names


async def test_login_refuses_bad_credentials(
    client: httpx.AsyncClient, account: Account
) -> None:
    wrong_password = await client.post(
        "/v1/auth/login", json={"email": account.user.email, "password": "not-it"}
    )
    assert wrong_password.status_code == 401

    unknown_email = await client.post(
        "/v1/auth/login", json={"email": "nobody@example.com", "password": "s3cret"}
    )
    assert unknown_email.status_code == 401
    # Indistinguishable bodies: account existence must not leak.
    assert wrong_password.json() == unknown_email.json()


async def test_login_refuses_deactivated_user(
    client: httpx.AsyncClient, account: Account
) -> None:
    async with DatabasePipe() as pipe:
        await pipe.users.set_active(account.user.id, False)

    response = await client.post(
        "/v1/auth/login", json={"email": account.user.email, "password": "s3cret"}
    )
    assert response.status_code == 401


async def test_cors_allows_the_webview_origin(client: httpx.AsyncClient) -> None:
    """The miniapp WebView fetches cross-origin; the wildcard must be served."""
    response = await client.get("/health", headers={"Origin": "http://localhost:3180"})
    assert response.headers.get("access-control-allow-origin") == "*"

    preflight = await client.options(
        "/v1/auth/login",
        headers={
            "Origin": "http://localhost:3180",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,authorization",
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers.get("access-control-allow-origin") == "*"


async def test_cors_never_allows_credentials(client: httpx.AsyncClient) -> None:
    """The cookie's CSRF defence: no other origin may send a credentialed write."""
    preflight = await client.options(
        "/v1/sessions",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-requested-with,content-type",
        },
    )
    assert preflight.headers.get("access-control-allow-origin") == "*"
    assert "access-control-allow-credentials" not in preflight.headers


async def test_use_stamps_last_used_at(client: httpx.AsyncClient, account: Account) -> None:
    """The stamp lands shortly after the response, not before it.

    The request's transaction commits when FastAPI tears down the `get_pipe`
    dependency, which can happen after the body has been written -- so this
    polls rather than reading once and assuming ordering it does not have.
    """
    async with DatabasePipe() as pipe:
        before = (await pipe.tokens.list_for_user(account.user.id))[0]
    assert before.last_used_at is None

    await client.get(PROTECTED, headers=account.headers)

    deadline = time.monotonic() + 5.0
    while True:
        async with DatabasePipe() as pipe:
            after = (await pipe.tokens.list_for_user(account.user.id))[0]
        if after.last_used_at is not None:
            break
        assert time.monotonic() < deadline, "last_used_at was never stamped"
        await asyncio.sleep(0.05)


async def _web_session(client: httpx.AsyncClient, account: Account) -> str:
    response = await client.post(
        "/v1/auth/session", json={"email": account.user.email, "password": "s3cret"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["email"] == account.user.email
    set_cookie = response.headers["set-cookie"]
    for flag in ("HttpOnly", "Secure", "SameSite=lax", "Path=/"):
        assert flag.lower() in set_cookie.lower()
    client.cookies.clear()  # the jar won't replay a Secure cookie over http; send it by hand
    return set_cookie.split(";", 1)[0]


async def test_cookie_signs_the_web_app_in_and_out(
    client: httpx.AsyncClient, account: Account
) -> None:
    cookie = await _web_session(client, account)
    headers = {"Cookie": cookie}
    assert (await client.get("/v1/me", headers=headers)).status_code == 200

    # Writes need the CSRF header on top of the cookie.
    refused = await client.post("/v1/sessions", json={}, headers=headers)
    assert refused.status_code == 403
    created = await client.post(
        "/v1/sessions", json={}, headers={**headers, "X-Requested-With": "context"}
    )
    assert created.status_code == 201, created.text

    assert (await client.delete("/v1/auth/session", headers=headers)).status_code == 403
    out = await client.delete(
        "/v1/auth/session", headers={**headers, "X-Requested-With": "context"}
    )
    assert out.status_code == 204
    assert "max-age=0" in out.headers["set-cookie"].lower()
    assert (await client.get("/v1/me", headers=headers)).status_code == 401


async def test_cookie_sign_in_rejects_a_wrong_password(
    client: httpx.AsyncClient, account: Account
) -> None:
    response = await client.post(
        "/v1/auth/session", json={"email": account.user.email, "password": "nope"}
    )
    assert response.status_code == 401
    assert "set-cookie" not in response.headers


async def test_the_header_wins_over_a_foreign_cookie(
    client: httpx.AsyncClient, account: Account, other_account: Account
) -> None:
    theirs = await _web_session(client, other_account)
    response = await client.get("/v1/me", headers={**account.headers, "Cookie": theirs})
    assert response.json()["user"]["id"] == str(account.user.id)
