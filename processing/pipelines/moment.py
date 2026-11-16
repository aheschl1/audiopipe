"""Tier: moments, the wall-clock stretches of a day, described and linked by an LLM.

Discovery merges every user's recording activity (audio sessions, photos,
clips) into windows bounded by ``moment_gap_ms`` of silence, across sessions.
A window becomes a job once it is closed and settled; its dedup key hashes
what it was built from, so late transcripts or keyframes rebuild it. A job
splits long windows, sends each moment's labelled items and images to the
model, and replaces the window's moments and their links in one transaction.
"""

import hashlib
import logging
import time
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from database.pipe import DatabasePipe
from database.repos.moments import LinkCreate, MomentCreate
from database.repos.pipelines.moment import ActivityRow, ItemRow, MomentQueries
from database.schema.jobs import Job, JobCreate
from processing.base import Pipeline
from processing.config import ProcessingSettings, get_settings
from processing.llm import Image, Llm, LlmError
from processing.moments import (
    SYSTEM_PROMPT,
    Activity,
    Item,
    Window,
    build_manifest,
    cover,
    parse_reply,
    render,
    span,
    split,
    windows,
)
from storage.pipe import BlobPipe

logger = logging.getLogger(__name__)


def closed_windows(
    rows: Sequence[ActivityRow], now: datetime, settings: ProcessingSettings
) -> list[tuple[UUID, Window]]:
    """Per user, the windows whose gap has passed and whose uploads have settled."""
    gap = timedelta(milliseconds=settings.moment_gap_ms)
    settle = timedelta(seconds=settings.moment_settle_seconds)
    by_user: dict[UUID, list[ActivityRow]] = defaultdict(list)
    for row in rows:
        by_user[row.user_id].append(row)
    out = []
    for user_id, user_rows in by_user.items():
        activity = [Activity(r.session_id, r.start_at, r.end_at) for r in user_rows]
        for window in windows(activity, settings.moment_gap_ms):
            members = [r for r in user_rows if r.session_id in window.session_ids]
            last_ingest = max(r.last_ingested_at for r in members)
            live = any(r.open and now - r.last_ingested_at < gap for r in members)
            if now - window.end >= gap and now - last_ingest >= settle and not live:
                out.append((user_id, window))
    return out


def fingerprint(window: Window, digests: Sequence[Any]) -> str:
    parts = [window.start.isoformat(), window.end.isoformat()]
    parts += [
        f"{d.session_id}:{d.segments}:{d.artifacts}:{d.last_artifact_at}"
        for d in sorted(digests, key=lambda d: str(d.session_id))
    ]
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


Speakers = dict[tuple[UUID, str], tuple[str | None, UUID | None]]


def to_item(row: ItemRow, names: Speakers) -> Item | None:
    start = row.started_at + timedelta(milliseconds=row.start_ms)
    end = None if row.end_ms is None else row.started_at + timedelta(milliseconds=row.end_ms)
    meta = row.metadata
    if row.kind == "transcript":
        text = str(meta.get("text") or "").strip()
        if not text:
            return None
        label = meta.get("speaker")
        name, speaker_id = names.get((row.session_id, label), (None, None)) if isinstance(label, str) else (None, None)
        return Item(
            row.id, "utterance", row.session_id, start, end, row.start_ms,
            text=text, speaker=name or (label if isinstance(label, str) else None),
            speaker_id=speaker_id,
        )
    if row.kind == "keyframe":
        segment = row.links.get("segment")
        return Item(
            row.id, "photo" if meta.get("reason") == "photo" else "keyframe",
            row.session_id, start, None, row.start_ms,
            segment_id=UUID(segment) if isinstance(segment, str) else None,
            offset_ms=int(meta.get("offset_ms") or 0),
            width=int(meta.get("width") or 0), height=int(meta.get("height") or 0),
            reason=str(meta.get("reason") or ""),
        )
    if row.kind == "sound-span":
        peak = meta.get("peak")
        return Item(
            row.id, "sound", row.session_id, start, end, row.start_ms,
            text=str(meta.get("label") or "sound"),
            score=float(peak) if isinstance(peak, (int, float)) else None,
        )
    return None


def snapshot(label: str, item: Item) -> dict[str, Any]:
    return {
        "label": label,
        "artifact_id": str(item.artifact_id),
        "kind": item.kind,
        "session_id": str(item.session_id),
        "at_ms": item.at_ms,
        "start_at": item.start.isoformat(),
        "end_at": item.end.isoformat() if item.end else None,
        "text": item.text,
        "speaker": item.speaker,
        "speaker_id": str(item.speaker_id) if item.speaker_id else None,
        "segment_id": str(item.segment_id) if item.segment_id else None,
        "offset_ms": item.offset_ms,
        "width": item.width,
        "height": item.height,
    }


class MomentPipeline(Pipeline):
    name = "moment"

    async def setup(self) -> None:
        self._settings = get_settings()
        self._llm = Llm(self._settings)
        self._last_discovery = 0.0
        tz = self._settings.moment_timezone
        self._tz = ZoneInfo(tz) if tz else None

    async def teardown(self) -> None:
        await self._llm.close()

    async def discover(self, limit: int) -> Sequence[JobCreate]:
        settings = get_settings()
        if not settings.llm_enabled:
            return ()
        # Recomputing every window is cheap but not free; moments are minutes late anyway.
        if time.monotonic() - getattr(self, "_last_discovery", 0.0) < settings.moment_discover_interval_seconds:
            return ()
        self._last_discovery = time.monotonic()
        async with DatabasePipe() as pipe:
            await pipe.moments.prune_orphans()
            queries = MomentQueries(pipe.connection)
            closed = closed_windows(
                await queries.activity(settings.moment_lookback_days), datetime.now(UTC), settings
            )
            sessions = sorted({s for _, w in closed for s in w.session_ids}, key=str)
            if not sessions:
                return ()
            busy = await queries.busy_sessions(sessions, self.name)
            digests = {d.session_id: d for d in await queries.digests(sessions)}
        settled_before = datetime.now(UTC) - timedelta(seconds=settings.moment_settle_seconds)
        jobs = []
        for user_id, window in closed:
            if window.session_ids & busy:
                continue
            members = [digests[s] for s in window.session_ids if s in digests]
            # Upstream tiers chain into each other; wait until they have gone quiet.
            if any(d.last_artifact_at and d.last_artifact_at > settled_before for d in members):
                continue
            key = fingerprint(window, members)
            jobs.append(
                JobCreate(
                    pipeline=self.name,
                    payload={
                        "user_id": str(user_id),
                        "start_at": window.start.isoformat(),
                        "end_at": window.end.isoformat(),
                        "session_ids": sorted(str(s) for s in window.session_ids),
                    },
                    dedup_key=f"{self.name}:{user_id}:{key}",
                )
            )
        if not jobs:
            return ()
        # Insert-time dedup is too late: settled windows would fill the batch
        # forever and starve new ones. Drop them here, and do the newest first.
        async with DatabasePipe() as pipe:
            done = await MomentQueries(pipe.connection).existing_keys(
                self.name, [j.dedup_key for j in jobs if j.dedup_key]
            )
        fresh = [j for j in jobs if j.dedup_key not in done]
        fresh.sort(key=lambda j: j.payload["start_at"], reverse=True)
        return fresh[:limit]

    async def process(self, job: Job) -> dict[str, Any]:
        s = self._settings
        user_id = UUID(job.payload["user_id"])
        start = datetime.fromisoformat(job.payload["start_at"])
        end = datetime.fromisoformat(job.payload["end_at"])
        session_ids = [UUID(x) for x in job.payload["session_ids"]]

        async with DatabasePipe() as pipe:
            queries = MomentQueries(pipe.connection)
            rows = await queries.items(session_ids, start, end)
            names = {
                (n.session_id, n.speaker): (n.name, n.speaker_id)
                for n in await queries.speaker_names(session_ids)
            }
        keys = {row.id: row.object_key for row in rows}
        items = [i for row in rows if (i := to_item(row, names)) is not None]

        built: list[tuple[MomentCreate, list[LinkCreate]]] = []
        usage: dict[str, int] = defaultdict(int)
        for group in split(items, s.moment_max_ms, s.moment_split_gap_ms):
            if not any(i.kind != "sound" for i in group):
                continue
            moment, links, used = await self._describe(user_id, group, keys)
            built.append((moment, links))
            for k, v in used.items():
                usage[k] += v

        async with DatabasePipe() as pipe:
            created = await pipe.moments.replace(user_id, start, end, built)
        return {
            "moments": len(created),
            "items": len(items),
            "links": sum(len(links) for _, links in built),
            "usage": dict(usage),
        }

    async def _describe(
        self, user_id: UUID, group: list[Item], keys: dict[UUID, str | None]
    ) -> tuple[MomentCreate, list[LinkCreate], dict[str, int]]:
        s = self._settings
        start, stop = span(group)
        manifest = build_manifest(
            group,
            max_images=s.moment_max_images,
            max_utterances=s.moment_max_utterances,
            max_sounds=s.moment_max_sounds,
            slack_ms=s.moment_candidate_slack_ms,
            min_sound_peak=s.moment_min_sound_peak,
        )
        images = []
        async with BlobPipe() as blobs:
            for label in manifest.images:
                key = keys.get(manifest.labels[label].artifact_id)
                if key:
                    images.append(Image(await blobs.get(key), f"[image {label}]"))
        text = render(manifest, start, stop, self._tz)
        reply = await self._llm.complete_json(SYSTEM_PROMPT, text, images)
        try:
            draft = parse_reply(reply.body, manifest.labels, s.moment_link_max_ms)
        except ValueError as exc:
            raise LlmError(str(exc)) from exc

        shown_images = [manifest.labels[label] for label in manifest.images]
        moment = MomentCreate(
            user_id=user_id,
            start_at=start,
            end_at=stop,
            title=draft.title,
            summary=draft.summary,
            session_ids=sorted({i.session_id for i in group}, key=str),
            model=reply.model,
            metadata={
                "people": list(draft.people),
                "topics": list(draft.topics),
                "visible_text": list(draft.visible_text),
                "items": [snapshot(label, item) for label, item in manifest.labels.items()],
                "cover": [str(i.artifact_id) for i in cover(shown_images, draft.links)],
                "counts": {
                    "utterances": manifest.total_utterances,
                    "frames": sum(1 for i in group if i.kind == "keyframe"),
                    "photos": sum(1 for i in group if i.kind == "photo"),
                    "links": len(draft.links),
                },
                "dropped_links": draft.dropped_links,
                "usage": reply.usage,
            },
        )
        links = [
            LinkCreate(
                from_artifact=link.from_id, to_artifact=link.to_id,
                relation=link.relation, confidence=link.confidence, why=link.why,
            )
            for link in draft.links
        ]
        return moment, links, reply.usage
