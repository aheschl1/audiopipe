"""Pure logic of the moment tier: windows, splitting, the LLM manifest and its reply.

A moment is a wall-clock stretch of recording, across sessions. Its items are
artifacts placed in it (utterances, keyframes, photos, sound spans), each
given a short label (U3, K2, P1, S1) the model refers to; links the model
returns are only kept between labels it was shown, within ``link_max_ms``.
"""

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, tzinfo
from typing import Any, Literal
from uuid import UUID

Kind = Literal["utterance", "keyframe", "photo", "sound"]
Relation = Literal["depicts", "mentions", "reads", "same_scene", "unrelated"]
RELATIONS: tuple[str, ...] = ("depicts", "mentions", "reads", "same_scene", "unrelated")
_PREFIX: dict[str, str] = {"utterance": "U", "keyframe": "K", "photo": "P", "sound": "S"}
_MAX_TEXT = 400
_MAX_CANDIDATES = 80
# Always present while recording, and already covered by the transcript.
_UNINFORMATIVE_SOUNDS = frozenset({"Speech", "Silence", "Conversation", "Narration, monologue"})


@dataclass(frozen=True)
class Activity:
    session_id: UUID
    start: datetime
    end: datetime


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime
    session_ids: frozenset[UUID]


@dataclass(frozen=True)
class Item:
    artifact_id: UUID
    kind: Kind
    session_id: UUID
    start: datetime
    end: datetime | None = None
    at_ms: int | None = None  # session-relative
    text: str | None = None
    speaker: str | None = None
    speaker_id: UUID | None = None
    segment_id: UUID | None = None
    offset_ms: int | None = None
    width: int | None = None
    height: int | None = None
    reason: str | None = None
    score: float | None = None  # a sound span's peak

    @property
    def stop(self) -> datetime:
        return self.end or self.start

    @property
    def is_image(self) -> bool:
        return self.kind in ("keyframe", "photo")


@dataclass(frozen=True)
class Link:
    from_id: UUID
    to_id: UUID
    relation: str
    confidence: float
    why: str


@dataclass(frozen=True)
class Draft:
    title: str
    summary: str
    people: tuple[str, ...]
    topics: tuple[str, ...]
    visible_text: tuple[str, ...]
    links: tuple[Link, ...]
    dropped_links: int = 0


@dataclass
class Manifest:
    labels: dict[str, Item]
    images: list[str]  # labels, in the order their images are attached
    candidates: list[tuple[str, str]]
    shown_utterances: int
    total_utterances: int
    extra: dict[str, Any] = field(default_factory=dict)


def windows(activity: Iterable[Activity], gap_ms: int) -> list[Window]:
    """Merge activity whose gaps are at most ``gap_ms`` into windows, in time order."""
    gap = timedelta(milliseconds=gap_ms)
    out: list[Window] = []
    for a in sorted(activity, key=lambda a: (a.start, a.end)):
        if out and a.start - out[-1].end <= gap:
            last = out[-1]
            out[-1] = Window(last.start, max(last.end, a.end), last.session_ids | {a.session_id})
        else:
            out.append(Window(a.start, max(a.start, a.end), frozenset({a.session_id})))
    return out


def split(items: Sequence[Item], max_ms: int, split_gap_ms: int) -> list[list[Item]]:
    """Groups of at most ``max_ms`` each, cut at the largest quiet gaps.

    Sound spans run long and would hide every gap, so groups are cut on the
    other items; each sound joins every group it overlaps.
    """
    points = sorted((i for i in items if i.kind != "sound"), key=lambda i: (i.start, i.stop))
    sounds = [i for i in items if i.kind == "sound"]
    groups = _cut(points, timedelta(milliseconds=max_ms), timedelta(milliseconds=split_gap_ms))
    out = []
    for group in groups:
        start, stop = group[0].start, max(i.stop for i in group)
        overlapping = [s for s in sounds if s.start <= stop and s.stop >= start]
        out.append(sorted(group + overlapping, key=lambda i: (i.start, i.kind)))
    return out


def _cut(points: list[Item], max_span: timedelta, min_gap: timedelta) -> list[list[Item]]:
    if not points:
        return []
    if max(i.stop for i in points) - points[0].start <= max_span or len(points) == 1:
        return [points]
    reach = points[0].stop
    best, best_gap = None, min_gap
    for index in range(1, len(points)):
        gap = points[index].start - reach
        if gap >= best_gap:
            best, best_gap = index, gap
        reach = max(reach, points[index].stop)
    if best is None:
        # No quiet gap: equal slices of time.
        start = points[0].start
        slices = math.ceil((max(i.stop for i in points) - start) / max_span)
        width = (max(i.stop for i in points) - start) / slices
        buckets: list[list[Item]] = [[] for _ in range(slices)]
        for item in points:
            buckets[min(slices - 1, int((item.start - start) / width))].append(item)
        return [b for b in buckets if b]
    return _cut(points[:best], max_span, min_gap) + _cut(points[best:], max_span, min_gap)


def pick_images(images: Sequence[Item], limit: int) -> list[Item]:
    """Every photo first, then keyframes evenly through time, up to ``limit``."""
    photos = [i for i in images if i.kind == "photo"]
    frames = [i for i in images if i.kind != "photo"]
    if len(photos) >= limit:
        chosen = _evenly(photos, limit)
    else:
        chosen = photos + _evenly(frames, limit - len(photos))
    return sorted(chosen, key=lambda i: i.start)


def _evenly(items: Sequence[Item], count: int) -> list[Item]:
    if count <= 0:
        return []
    if len(items) <= count:
        return list(items)
    if count == 1:
        return [items[len(items) // 2]]
    return [items[round(j * (len(items) - 1) / (count - 1))] for j in range(count)]


def span(group: Sequence[Item]) -> tuple[datetime, datetime]:
    """A moment's extent: its non-sound items, as sound spans can run past it."""
    core = [i for i in group if i.kind != "sound"] or list(group)
    return min(i.start for i in core), max(i.stop for i in core)


def alias_speakers(utterances: Sequence[Item]) -> list[Item]:
    """Unnamed diarizer labels become "voice 1", "voice 2"... in order of first speech."""
    aliases: dict[str, str] = {}
    out = []
    for item in utterances:
        speaker = item.speaker
        if speaker and "SPEAKER_" in speaker:
            speaker = aliases.setdefault(speaker, f"voice {len(aliases) + 1}")
        out.append(replace(item, speaker=speaker))
    return out


def informative_sounds(sounds: Iterable[Item], min_peak: float, limit: int) -> list[Item]:
    """The confident, non-speech sound spans, strongest first, then in time order."""
    kept = [
        s for s in sounds
        if (s.score or 0) >= min_peak and (s.text or "") not in _UNINFORMATIVE_SOUNDS
    ]
    kept.sort(key=lambda s: -(s.score or 0))
    return sorted(kept[:limit], key=lambda s: s.start)


def build_manifest(
    items: Sequence[Item],
    *,
    max_images: int,
    max_utterances: int,
    max_sounds: int,
    slack_ms: int,
    min_sound_peak: float = 0.0,
) -> Manifest:
    utterances = alias_speakers(sorted((i for i in items if i.kind == "utterance"), key=lambda i: i.start))
    shown = _evenly(utterances, max_utterances)
    sounds = informative_sounds((i for i in items if i.kind == "sound"), min_sound_peak, max_sounds)
    images = pick_images([i for i in items if i.is_image], max_images)

    labels: dict[str, Item] = {}
    for kind, group in (("utterance", shown), ("keyframe", images), ("sound", sounds)):
        counters: dict[str, int] = {}
        for item in sorted(group, key=lambda i: i.start):
            prefix = _PREFIX[item.kind if kind == "keyframe" else kind]
            counters[prefix] = counters.get(prefix, 0) + 1
            labels[f"{prefix}{counters[prefix]}"] = item

    slack = timedelta(milliseconds=slack_ms)
    image_labels = [label for label, item in labels.items() if item.is_image]
    candidates = []
    for image_label in image_labels:
        image = labels[image_label]
        for label, other in labels.items():
            if other.kind == "utterance" and other.start - slack <= image.start <= other.stop + slack:
                candidates.append((image_label, label))
    return Manifest(
        labels=labels,
        images=image_labels,
        candidates=candidates[:_MAX_CANDIDATES],
        shown_utterances=len(shown),
        total_utterances=len(utterances),
    )


SYSTEM_PROMPT = """\
You are building a memory of one person's day from what their phone and smart \
glasses recorded. You get one "moment": a stretch of time bounded by a gap in \
recording. Items are labelled: U = transcript utterance, K = video keyframe, \
P = photo, S = sound span. Each K and P has its image attached, introduced by \
its label. Times are local clock time.

Answer with a JSON object:
- "title": up to 8 words.
- "summary": 2 to 4 sentences: what happened and with whom. Be literal; \
never invent names that are not spoken or written in the items.
- "people": the participants, by the speaker labels or names actually heard.
- "topics": short topic tags (1 to 3 words each, at most 5) for what the \
moment is about, e.g. "budget review", "hiking". Empty when nothing applies.
- "visible_text": text read from the images that matters, verbatim.
- "links": how the items relate. Each link is {"from": label, "to": label, \
"relation": one of the relations below, "confidence": 0 to 1, "why": a short \
reason}. Relations:
    depicts     an image shows the scene or people of an utterance or sound
    mentions    an utterance talks about something visible in an image
    reads       an utterance reads out or discusses text in an image
    same_scene  two images show the same place or activity
    unrelated   a CANDIDATE pair that overlaps in time but is not connected
  Use only labels listed in the items. Judge every CANDIDATE pair; add other \
links only where the evidence is clear.
"""


def _clock(at: datetime, tz: tzinfo | None) -> str:
    return at.astimezone(tz).strftime("%H:%M:%S")


def _clip(text: str | None) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= _MAX_TEXT else text[: _MAX_TEXT - 1] + "…"


def render(manifest: Manifest, start: datetime, end: datetime, tz: tzinfo | None) -> str:
    """The user message: the moment's items as labelled lines."""
    local = start.astimezone(tz)
    lines = [
        f"MOMENT  {local:%A %Y-%m-%d} {_clock(start, tz)} to {_clock(end, tz)}"
        f" ({_duration(end - start)})",
        "",
    ]
    by_prefix: dict[str, list[tuple[str, Item]]] = {}
    for label, item in manifest.labels.items():
        by_prefix.setdefault(label[0], []).append((label, item))

    heading = "UTTERANCES"
    if manifest.shown_utterances < manifest.total_utterances:
        heading += f" ({manifest.shown_utterances} of {manifest.total_utterances}, evenly sampled)"
    sections = (
        ("U", heading, lambda l, i: f'{l}  {_clock(i.start, tz)}-{_clock(i.stop, tz)}  {i.speaker or "unknown"}  "{_clip(i.text)}"'),
        ("S", "SOUND SPANS", lambda l, i: f"{l}  {_clock(i.start, tz)}-{_clock(i.stop, tz)}  {_clip(i.text)}"
            + (f" (peak {i.score:.2f})" if i.score is not None else "")),
    )
    for prefix, title, fmt in sections:
        if by_prefix.get(prefix):
            lines += [title] + [fmt(l, i) for l, i in by_prefix[prefix]] + [""]
    if manifest.images:
        lines.append("IMAGES (attached below in this order)")
        for label in manifest.images:
            item = manifest.labels[label]
            source = "photo" if item.kind == "photo" else f"video frame +{(item.offset_ms or 0) / 1000:.1f}s"
            lines.append(f"{label}  {_clock(item.start, tz)}  {source}")
        lines.append("")
    if manifest.candidates:
        lines.append("CANDIDATES (overlap in time; judge each)")
        lines += [f"{a} ~ {b}" for a, b in manifest.candidates]
        lines.append("")
    if not manifest.labels:
        lines.append("(no items)")
    return "\n".join(lines).rstrip() + "\n"


def _duration(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes}m {seconds}s"


def _strings(value: Any, limit: int = 20) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return ()
    return tuple(v.strip() for v in value if isinstance(v, str) and v.strip())[:limit]


def parse_reply(body: Mapping[str, Any], labels: Mapping[str, Item], link_max_ms: int) -> Draft:
    """The model's reply as a draft; malformed or unverifiable links are dropped."""
    summary = body.get("summary")
    summary = summary.strip() if isinstance(summary, str) else ""
    title = body.get("title")
    title = title.strip() if isinstance(title, str) and title.strip() else ""
    if not title and not summary:
        raise ValueError("reply has neither a title nor a summary")
    if not title:
        title = " ".join(summary.split()[:8])

    max_gap = timedelta(milliseconds=link_max_ms)
    kept: dict[tuple[UUID, UUID, str], Link] = {}
    raw = body.get("links")
    raw = raw if isinstance(raw, list) else []
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        a, b = labels.get(str(entry.get("from", ""))), labels.get(str(entry.get("to", "")))
        relation = entry.get("relation")
        if a is None or b is None or a is b or relation not in RELATIONS:
            continue
        gap = max(a.start - b.stop, b.start - a.stop, timedelta(0))
        if gap > max_gap:
            continue
        confidence = entry.get("confidence")
        confidence = (
            min(1.0, max(0.0, float(confidence)))
            if isinstance(confidence, (int, float)) and math.isfinite(confidence)
            else 0.5
        )
        why = entry.get("why")
        link = Link(a.artifact_id, b.artifact_id, relation, confidence, why.strip()[:300] if isinstance(why, str) else "")
        key = (link.from_id, link.to_id, relation)
        if key not in kept or kept[key].confidence < confidence:
            kept[key] = link
    return Draft(
        title=title[:120],
        summary=summary,
        people=_strings(body.get("people")),
        topics=_strings(body.get("topics"), 5),
        visible_text=_strings(body.get("visible_text"), 40),
        links=tuple(kept.values()),
        dropped_links=len(raw) - len(kept),
    )


def cover(images: Sequence[Item], links: Sequence[Link], count: int = 4) -> list[Item]:
    """Representative images: photos first, then the most-linked frames, in time order."""
    degree: dict[UUID, int] = {}
    for link in links:
        if link.relation != "unrelated":
            degree[link.from_id] = degree.get(link.from_id, 0) + 1
            degree[link.to_id] = degree.get(link.to_id, 0) + 1
    ranked = sorted(
        images,
        key=lambda i: (i.kind != "photo", -degree.get(i.artifact_id, 0), i.start),
    )
    return sorted(ranked[:count], key=lambda i: i.start)
