from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from database.repos.moments import Moment
from database.schema.automations import Automation
from processing.automations import parse, prefilter, render

T0 = datetime(2026, 11, 14, 15, 0, tzinfo=UTC)


def rule(conditions=None, actions=None) -> Automation:
    return Automation.model_validate({
        "id": uuid4(), "user_id": uuid4(), "name": "r",
        "conditions": conditions or {},
        "actions": actions or [{"type": "extract", "fields": [{"name": "due"}]}],
        "active_since": T0, "created_at": T0, "updated_at": T0,
    })


def moment(minutes: int = 10, **metadata) -> Moment:
    return Moment(
        id=uuid4(), user_id=uuid4(), start_at=T0, end_at=T0 + timedelta(minutes=minutes),
        title="Standup", summary="Went over the plan.", session_ids=[], model="m",
        metadata={"people": ["Sam"], "topics": ["Work"], "items": [
            {"kind": "utterance", "text": "ship it Friday", "speaker": "Ana", "speaker_id": "x"},
            {"kind": "utterance", "text": "ok", "speaker": "voice 1", "speaker_id": None},
        ], **metadata},
        created_at=T0,
    )


@pytest.mark.parametrize(("conditions", "reason"), [
    ({}, None),
    ({"people": ["ana"]}, None),
    ({"people": ["voice 1"]}, "none of the people were there"),
    ({"topics": ["work"]}, None),
    ({"topics": ["food"]}, "none of the topics came up"),
    ({"keywords": ["FRIDAY"]}, None),
    ({"keywords": ["monday"]}, "none of the keywords were said"),
    ({"keywords": ["it fri"]}, "none of the keywords were said"),
    ({"keywords": ["plan"]}, None),
    ({"keywords": ["pla"]}, "none of the keywords were said"),
    ({"min_minutes": 15}, "shorter than 15 minutes"),
])
def test_prefilter(conditions, reason) -> None:
    assert prefilter(rule(conditions), moment()) == reason


def test_parse_keeps_only_asked_fields_as_strings() -> None:
    r = rule({"ask": "q"}, [{"type": "extract", "fields": [{"name": "due"}, {"name": "who"}]}])
    verdict = parse({"applies": True, "reason": "said so", "fields": {"due": 5, "who": ["Ana"], "x": "y"}}, r)
    assert verdict.applies and verdict.fields == {"due": "5", "who": '["Ana"]'}
    assert parse({"applies": True, "fields": {"due": ""}}, r).fields == {"due": None, "who": None}
    assert not parse({"applies": False}, r).applies
    with pytest.raises(ValueError):
        parse({"fields": {}}, r)
    assert parse({}, rule()).applies


def test_render_lists_the_rule_then_the_moment() -> None:
    r = rule({"ask": "Is a date set?"}, [{"type": "extract", "fields": [{"name": "due", "description": "the day"}]}])
    text = render(r, moment(), UTC)
    assert 'Condition: Is a date set?\nFields:\n- "due": the day\n' in text
    assert "Saturday 2026-11-14 15:00 to 15:10 (UTC)" in text
    assert "TRANSCRIPT\nAna: ship it Friday\nvoice 1: ok\n" in text
