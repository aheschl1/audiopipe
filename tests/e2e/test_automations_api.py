"""Automation rules over the API: validation, ownership, and the active window."""

from datetime import UTC, datetime, timedelta

import httpx

from database.pipe import DatabasePipe
from database.repos.moments import Moment, MomentCreate
from tests.e2e.conftest import Account, make_session

RULE = {
    "name": "Deadlines",
    "conditions": {"keywords": ["deadline"], "ask": "Is a deadline agreed?"},
    "actions": [
        {"type": "extract", "fields": [{"name": "due", "description": "the date"}]},
    ],
}


async def _moment(account: Account, title: str, hours_ago: int) -> Moment:
    session = await make_session(account)
    now = datetime.now(UTC) - timedelta(hours=hours_ago)
    async with DatabasePipe() as pipe:
        (moment,) = await pipe.moments.replace(
            account.user.id, now, now + timedelta(minutes=5),
            [(MomentCreate(
                user_id=account.user.id, start_at=now, end_at=now + timedelta(minutes=5),
                title=title, summary="", session_ids=[session.id], model="m", metadata={},
            ), [])],
        )
    return moment


async def test_create_read_update_delete(client: httpx.AsyncClient, account: Account) -> None:
    created = await client.post("/v1/automations", json=RULE, headers=account.headers)
    assert created.status_code == 201, created.text
    rule = created.json()
    assert rule["trigger"] == "moment" and rule["enabled"] is True
    assert rule["actions"][0]["fields"] == [{"name": "due", "description": "the date"}]

    listed = (await client.get("/v1/automations", headers=account.headers)).json()
    assert [r["id"] for r in listed] == [rule["id"]]

    paused = await client.put(
        f"/v1/automations/{rule['id']}", json={**RULE, "enabled": False}, headers=account.headers
    )
    assert paused.json()["enabled"] is False
    assert paused.json()["active_since"] == rule["active_since"]
    resumed = await client.put(f"/v1/automations/{rule['id']}", json=RULE, headers=account.headers)
    since = datetime.fromisoformat
    assert since(resumed.json()["active_since"]) > since(rule["active_since"])

    gone = await client.delete(f"/v1/automations/{rule['id']}", headers=account.headers)
    assert gone.status_code == 204
    missing = await client.get(f"/v1/automations/{rule['id']}", headers=account.headers)
    assert missing.status_code == 404


async def test_bad_rules_are_refused(client: httpx.AsyncClient, account: Account) -> None:
    for bad in (
        {**RULE, "actions": []},
        {**RULE, "actions": [{"type": "webhook", "url": "https://x.example"}]},
        {**RULE, "actions": [{"type": "extract", "fields": [{"name": "a"}, {"name": "A"}]}]},
        {**RULE, "actions": [{"type": "email", "to": "x"}]},
        {**RULE, "trigger": "photo"},
        {**RULE, "conditions": {"colour": "red"}},
    ):
        response = await client.post("/v1/automations", json=bad, headers=account.headers)
        assert response.status_code == 422, (bad, response.text)


async def test_rules_are_private(
    client: httpx.AsyncClient, account: Account, other_account: Account
) -> None:
    rule = (await client.post("/v1/automations", json=RULE, headers=account.headers)).json()
    url = f"/v1/automations/{rule['id']}"
    assert (await client.get(url, headers=other_account.headers)).status_code == 404
    assert (await client.put(url, json=RULE, headers=other_account.headers)).status_code == 404
    assert (await client.delete(url, headers=other_account.headers)).status_code == 404
    assert (await client.get("/v1/automations", headers=other_account.headers)).json() == []


async def test_runs_list_and_show_on_the_moment(
    client: httpx.AsyncClient, account: Account, other_account: Account
) -> None:
    rule = (await client.post("/v1/automations", json=RULE, headers=account.headers)).json()
    matched, skipped = await _moment(account, "Planning", 2), await _moment(account, "Lunch", 1)
    async with DatabasePipe() as pipe:
        await pipe.automations.record_run(rule["id"], matched.id, "matched", {"fields": {"due": "Friday"}})
        await pipe.automations.record_run(rule["id"], skipped.id, "skipped", {"reason": "no keyword"})

    runs = (await client.get("/v1/automations/runs", headers=account.headers)).json()
    assert [(r["moment_title"], r["fields"]) for r in runs] == [("Planning", {"due": "Friday"})]
    every = (
        await client.get("/v1/automations/runs", params={"automation_id": rule["id"]}, headers=account.headers)
    ).json()
    assert {r["status"] for r in every} == {"matched", "skipped"}
    assert (await client.get("/v1/automations/runs", headers=other_account.headers)).json() == []

    detail = (await client.get(f"/v1/moments/{matched.id}", headers=account.headers)).json()
    assert detail["automations"] == [
        {"automation_id": rule["id"], "name": "Deadlines", "fields": {"due": "Friday"}}
    ]
    plain = (await client.get(f"/v1/moments/{skipped.id}", headers=account.headers)).json()
    assert plain["automations"] == []
