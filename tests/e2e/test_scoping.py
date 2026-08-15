"""One caller must never see another's data, and must not learn that it exists."""

import httpx

from tests.e2e.conftest import Account, ingest, make_session
from tests.wav import wav_bytes


async def test_another_users_rows_are_invisible_and_unconfirmable(
    client: httpx.AsyncClient, account: Account, other_account: Account
) -> None:
    theirs = await make_session(other_account, label="not yours")
    their_segment = await ingest(theirs.id, wav_bytes())

    # 404 rather than 403 throughout: a 403 would confirm the id exists.
    for path in (
        f"/v1/sessions/{theirs.id}",
        f"/v1/sessions/{theirs.id}/segments",
        f"/v1/segments/{their_segment.id}",
        f"/v1/segments/{their_segment.id}/media",
    ):
        response = await client.get(path, headers=account.headers)
        assert response.status_code == 404, path

    # ...and the same ids are reachable by their actual owner.
    owner = await client.get(f"/v1/sessions/{theirs.id}", headers=other_account.headers)
    assert owner.status_code == 200


async def test_listings_only_contain_your_own_rows(
    client: httpx.AsyncClient, account: Account, other_account: Account
) -> None:
    mine = await make_session(account, label="mine")
    await ingest(mine.id, wav_bytes())
    theirs = await make_session(other_account, label="theirs")
    await ingest(theirs.id, wav_bytes())

    sessions = (await client.get("/v1/sessions", headers=account.headers)).json()
    assert [item["id"] for item in sessions["items"]] == [str(mine.id)]

    segments = (await client.get("/v1/segments", headers=account.headers)).json()
    assert {item["session_id"] for item in segments["items"]} == {str(mine.id)}


async def test_usage_counts_only_your_own_audio(
    client: httpx.AsyncClient, account: Account, other_account: Account
) -> None:
    theirs = await make_session(other_account)
    await ingest(theirs.id, wav_bytes())
    mine = await make_session(account)
    payload = wav_bytes(seconds=0.3)
    await ingest(mine.id, payload)

    me = (await client.get("/v1/me", headers=account.headers)).json()
    assert me["usage"] == [
        {"resource": "audio", "segments": 1, "total_bytes": len(payload)}
    ]
