"""Media routes authenticate like every other route: a bearer header or the
web sign-in cookie. Tokens in the URL are not accepted."""

import httpx

from tests.e2e.conftest import Account, ingest, make_session
from tests.wav import wav_bytes


async def _session_with_audio(account: Account):
    session = await make_session(account)
    await ingest(session.id, wav_bytes(seconds=0.5), duration_ms=500)
    return session


async def _cookie(client: httpx.AsyncClient, account: Account) -> dict[str, str]:
    response = await client.post(
        "/v1/auth/session", json={"email": account.user.email, "password": "s3cret"}
    )
    assert response.status_code == 200
    client.cookies.clear()  # a Secure cookie isn't replayed over http; send it by hand
    return {"Cookie": response.headers["set-cookie"].split(";", 1)[0]}


async def test_the_sign_in_cookie_streams_audio_with_ranges(
    client: httpx.AsyncClient, account: Account
) -> None:
    session = await _session_with_audio(account)
    headers = await _cookie(client, account)
    url = f"/v1/sessions/{session.id}/resources/audio/media"

    full = await client.get(url, headers=headers)
    assert full.status_code == 200
    assert full.headers["content-type"] == "audio/wav"
    ranged = await client.get(url, headers={**headers, "Range": "bytes=0-99"})
    assert ranged.status_code == 206
    assert len(ranged.content) == 100


async def test_a_token_in_the_url_is_not_a_credential(
    client: httpx.AsyncClient, account: Account
) -> None:
    session = await _session_with_audio(account)
    url = f"/v1/sessions/{session.id}/resources/audio/media"
    assert (await client.get(url)).status_code == 401
    assert (await client.get(url, params={"token": account.token})).status_code == 401
    assert (await client.get(url, headers=account.headers)).status_code == 200


async def test_another_users_cookie_gets_404(
    client: httpx.AsyncClient, account: Account, other_account: Account
) -> None:
    session = await _session_with_audio(account)
    theirs = await _cookie(client, other_account)
    response = await client.get(f"/v1/sessions/{session.id}/resources/audio/media", headers=theirs)
    assert response.status_code == 404
