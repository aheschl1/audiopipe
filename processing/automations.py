"""Pure parts of the automation tier: condition checks, the prompt, the reply.

Cheap conditions (people, topics, keywords, length) run first, so the model is
only asked about moments that could match. One model call answers the rule's
``ask`` question and fills every field together.
"""

import json
import re
from dataclasses import dataclass
from datetime import tzinfo
from typing import Any

from database.repos.moments import Moment
from database.schema.automations import Automation

SYSTEM_PROMPT = """\
You check one moment of a person's day, recorded by their phone or glasses,
against an automation rule they wrote. Reply with one JSON object:

{"applies": true or false, "reason": "one short sentence", "fields": {"<field name>": "value" or null}}

- "applies": whether the rule's condition holds for this moment. If the rule
  has no condition, it applies.
- "reason": why, citing what was said or seen.
- "fields": one entry per requested field, under its exact name. Give a short
  plain value taken from the moment, or null when the moment doesn't say.
  Don't guess.

The transcript is machine-made and may have errors.
"""

MAX_TRANSCRIPT_LINES = 400


@dataclass(frozen=True)
class Verdict:
    applies: bool
    reason: str
    fields: dict[str, str | None]


def people(moment: Moment) -> list[str]:
    """Names the model gave participants, plus named speakers in the transcript."""
    names = [str(p) for p in moment.metadata.get("people") or []]
    for item in moment.metadata.get("items") or []:
        speaker = item.get("speaker")
        if item.get("kind") == "utterance" and item.get("speaker_id") and speaker:
            names.append(str(speaker))
    return list(dict.fromkeys(names))


def transcript(moment: Moment) -> list[str]:
    lines = []
    for item in moment.metadata.get("items") or []:
        if item.get("kind") == "utterance" and item.get("text"):
            lines.append(f"{item.get('speaker') or 'unknown'}: {item['text']}")
    return lines


def _any(wanted: list[str], have: list[str]) -> bool:
    have = [h.casefold() for h in have]
    return any(w.casefold() in have for w in wanted)


def prefilter(rule: Automation, moment: Moment) -> str | None:
    """Why the cheap conditions fail, or None when they all hold."""
    c = rule.conditions
    if c.people and not _any(c.people, people(moment)):
        return "none of the people were there"
    if c.topics and not _any(c.topics, [str(t) for t in moment.metadata.get("topics") or []]):
        return "none of the topics came up"
    if c.keywords:
        text = "\n".join([moment.title, moment.summary, *transcript(moment)])
        if not any(re.search(rf"(?<!\w){re.escape(k)}(?!\w)", text, re.IGNORECASE) for k in c.keywords):
            return "none of the keywords were said"
    if c.min_minutes is not None and (moment.end_at - moment.start_at).total_seconds() < c.min_minutes * 60:
        return f"shorter than {c.min_minutes:g} minutes"
    return None


def wanted_fields(rule: Automation) -> list[tuple[str, str]]:
    return [(f.name, f.description) for action in rule.actions for f in action.fields]


def render(rule: Automation, moment: Moment, tz: tzinfo | None) -> str:
    start = moment.start_at.astimezone(tz)
    end = moment.end_at.astimezone(tz)
    lines = [
        "RULE",
        f"Condition: {rule.conditions.ask or '(none)'}",
    ]
    fields = wanted_fields(rule)
    if fields:
        lines.append("Fields:")
        lines += [f'- "{name}"' + (f": {desc}" if desc else "") for name, desc in fields]
    lines += [
        "",
        "MOMENT",
        f"{start:%A %Y-%m-%d %H:%M} to {end:%H:%M} ({start:%Z})",
        f"Title: {moment.title}",
        f"Summary: {moment.summary}",
    ]
    for key, label in (("people", "People"), ("topics", "Topics"), ("visible_text", "Visible text")):
        values = [str(v) for v in moment.metadata.get(key) or []]
        if values:
            lines.append(f"{label}: {'; '.join(values)}")
    said = transcript(moment)
    if said:
        lines += ["", "TRANSCRIPT", *said[:MAX_TRANSCRIPT_LINES]]
        if len(said) > MAX_TRANSCRIPT_LINES:
            lines.append(f"({len(said) - MAX_TRANSCRIPT_LINES} more lines not shown)")
    return "\n".join(lines) + "\n"


def parse(body: dict[str, Any], rule: Automation) -> Verdict:
    """The model's verdict; a rule without an ask always applies."""
    applies = body.get("applies")
    if rule.conditions.ask and not isinstance(applies, bool):
        raise ValueError(f"reply has no boolean 'applies': {body}")
    raw = body.get("fields") if isinstance(body.get("fields"), dict) else {}
    fields: dict[str, str | None] = {}
    for name, _ in wanted_fields(rule):
        value = raw.get(name)
        if value is None or value == "":
            fields[name] = None
        else:
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            fields[name] = text[:2000]
    return Verdict(
        applies=applies if rule.conditions.ask else True,
        reason=str(body.get("reason") or "")[:500],
        fields=fields,
    )
