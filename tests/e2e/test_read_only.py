"""A read-only user (the public demo account) can read everything it owns and change nothing."""

import json

import httpx
import pytest
import websockets
from websockets.exceptions import InvalidStatus

from tests.e2e.conftest import Account, make_session
from tests.e2e.test_stream import _url


async def _make_read_only(account: Account) -> None:
    from database.pipe import DatabasePipe

    async with DatabasePipe() as pipe:
        await pipe.users.set_read_only(account.user.id, True)


async def test_reads_pass_and_writes_are_refused(
    client: httpx.AsyncClient, account: Account
) -> None:
    session = await make_session(account, label="before")
    await _make_read_only(account)

    me = await client.get("/v1/me", headers=account.headers)
    assert me.status_code == 200
    assert me.json()["user"]["read_only"] is True
    assert (await client.get(f"/v1/sessions/{session.id}", headers=account.headers)).status_code == 200

    renamed = await client.post(
        f"/v1/sessions/{session.id}/label", json={"label": "after"}, headers=account.headers
    )
    assert renamed.status_code == 403
    created = await client.post("/v1/sessions", json={}, headers=account.headers)
    assert created.status_code == 403
    still = await client.get(f"/v1/sessions/{session.id}", headers=account.headers)
    assert still.json()["label"] == "before"


async def test_a_read_only_user_can_sign_in_and_out_on_the_web(
    client: httpx.AsyncClient, account: Account
) -> None:
    await _make_read_only(account)
    signed_in = await client.post(
        "/v1/auth/session", json={"email": account.user.email, "password": "s3cret"}
    )
    assert signed_in.status_code == 200
    assert signed_in.json()["read_only"] is True
    cookie = {"Cookie": signed_in.headers["set-cookie"].split(";", 1)[0]}
    client.cookies.clear()
    out = await client.delete(
        "/v1/auth/session", headers={**cookie, "X-Requested-With": "context"}
    )
    assert out.status_code == 204


async def test_a_read_only_user_cannot_stream(server: str, account: Account) -> None:
    session = await make_session(account)
    await _make_read_only(account)
    with pytest.raises(InvalidStatus) as refused:
        async with websockets.connect(_url(server, session.id), additional_headers=account.headers) as ws:
            await ws.send(json.dumps({"type": "hello", "version": 2}))
    assert refused.value.response.status_code == 403
