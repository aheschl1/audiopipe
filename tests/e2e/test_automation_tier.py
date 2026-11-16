"""The automation tier through the worker, against the stub chat model."""

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx

from database.pipe import DatabasePipe
from database.repos.moments import MomentCreate
from database.schema.automations import AutomationWrite
from tests.e2e.conftest import Account, make_session

START = datetime(2026, 11, 14, 15, 0, tzinfo=UTC)


async def _moment(account: Account, said: str, minutes: int = 30) -> Any:
    session = await make_session(account)
    end = START + timedelta(minutes=minutes)
    item = {
        "artifact_id": str(uuid4()), "label": "U1", "kind": "utterance", "session_id": str(session.id),
        "at_ms": 0, "start_at": START.isoformat(), "end_at": None, "text": said, "speaker": "Sam",
        "speaker_id": str(uuid4()), "segment_id": None, "offset_ms": None, "width": None, "height": None,
    }
    async with DatabasePipe() as pipe:
        (moment,) = await pipe.moments.replace(
            account.user.id, START, end,
            [(MomentCreate(
                user_id=account.user.id, start_at=START, end_at=end, title="Planning",
                summary="Talked it over.", session_ids=[session.id], model="m",
                metadata={"people": ["Sam"], "topics": ["work"], "items": [item]},
            ), [])],
        )
    return moment


async def _rule(account: Account, **spec: Any) -> Any:
    async with DatabasePipe() as pipe:
        return await pipe.automations.create(account.user.id, AutomationWrite.model_validate(spec))


async def _run_for(account: Account, moment_id: Any, automation_id: Any, timeout: float = 30.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async with DatabasePipe() as pipe:
            for run in await pipe.automations.runs(account.user.id, automation_id=automation_id):
                if run.moment_id == moment_id:
                    return run
        await asyncio.sleep(0.2)
    raise AssertionError("no run")


EXTRACT = [{"type": "extract", "fields": [{"name": "due", "description": "when"}]}]


async def test_rules_match_and_fill_fields(
    worker: None, client: httpx.AsyncClient, account: Account
) -> None:
    deadline = await _rule(
        account, name="Deadlines", conditions={"people": ["sam"], "ask": "Is a deadline set?"}, actions=EXTRACT
    )
    long = await _rule(account, name="Long", conditions={"min_minutes": 60}, actions=EXTRACT)

    moment = await _moment(account, "the deadline is Friday")
    matched = await _run_for(account, moment.id, deadline.id)
    assert (matched.status, matched.output["fields"]) == ("matched", {"due": "stub due"})
    skipped = await _run_for(account, moment.id, long.id)
    assert (skipped.status, skipped.output) == ("skipped", {"reason": "shorter than 60 minutes"})

    detail = (await client.get(f"/v1/moments/{moment.id}", headers=account.headers)).json()
    assert detail["automations"] == [
        {"automation_id": str(deadline.id), "name": "Deadlines", "fields": {"due": "stub due"}}
    ]

    # A rebuild is a new moment row, and runs again.
    rebuilt = await _moment(account, "the deadline is Friday, confirmed")
    assert (await _run_for(account, rebuilt.id, deadline.id)).status == "matched"


async def test_the_model_can_say_no(worker: None, account: Account) -> None:
    rule = await _rule(account, name="Ask", conditions={"ask": "Is a deadline set?"}, actions=EXTRACT)
    moment = await _moment(account, "just lunch")
    run = await _run_for(account, moment.id, rule.id)
    assert (run.status, run.output["reason"]) == ("skipped", "stub verdict")
