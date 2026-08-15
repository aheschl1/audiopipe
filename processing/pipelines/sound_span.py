"""Tier: merge the tagger's windows into few long spans of one class each.

Spans of different classes overlap freely; spans of one class never do, which
is what lets a class render as a single timeline lane. Span edges are the
union of their member windows, so they are smeared by up to one window in
either direction — an edge is not an event onset.
"""

import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from database.pipe import DatabasePipe
from database.repos.pipelines.sound_span import SoundSpanQueries
from database.schema.artifacts import ArtifactCreate
from database.schema.jobs import Job, JobCreate
from processing.base import Pipeline
from processing.config import get_settings
from resources import Resource

logger = logging.getLogger(__name__)

_SOURCE_PIPELINE = "audio-tag"

# ~12 days of session at the 5s hop; the tier holds every window in memory.
_MAX_WINDOWS = 200_000


@dataclass(frozen=True, slots=True)
class ScoredWindow:
    """One upstream ``audio-tag`` window, reduced to what span building needs."""

    start_ms: int
    end_ms: int
    labels: tuple[tuple[str, float], ...]


@dataclass(frozen=True, slots=True)
class ClassSpan:
    """A continuous stretch over which one class held."""

    label: str
    start_ms: int
    end_ms: int
    windows: int
    peak: float
    mean: float


@dataclass(frozen=True, slots=True)
class ClassSummary:
    """What one class contributed, for the map's ranked class list."""

    label: str
    spans: int
    total_ms: int
    peak: float


def read_window(
    start_ms: int | None, end_ms: int | None, metadata: Mapping[str, Any]
) -> ScoredWindow | None:
    """One artifact row as a window, or ``None`` if it can't be one."""
    if start_ms is None or end_ms is None or end_ms <= start_ms:
        return None
    raw = metadata.get("labels")
    labels = []
    if isinstance(raw, list):
        # Unusable labels are dropped, not raised on: one bad row must not
        # dead-letter a session's spans.
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            label, score = item.get("label"), item.get("score")
            if (
                isinstance(label, str)
                and label
                and isinstance(score, (int, float))
                and not isinstance(score, bool)
                and math.isfinite(score)
            ):
                labels.append((label, float(score)))
    return ScoredWindow(start_ms=start_ms, end_ms=end_ms, labels=tuple(labels))


def build_spans(
    windows: Sequence[ScoredWindow],
    *,
    enter: float,
    sustain: float,
    bridge_gap_ms: int,
    min_windows: int,
) -> list[ClassSpan]:
    """Every class's high-confidence stretches, one pass per class.

    A window where the class is absent scores 0 — audio-tag drops labels under
    its window floor, so ``sustain`` must sit above that floor.
    """
    ordered = _deduped(windows)
    labels = sorted({label for window in ordered for label, _ in window.labels})
    spans: list[ClassSpan] = []
    for label in labels:
        spans += _spans_for(
            label,
            ordered,
            enter=enter,
            sustain=sustain,
            bridge_gap_ms=bridge_gap_ms,
            min_windows=min_windows,
        )
    return sorted(spans, key=lambda span: (span.start_ms, span.end_ms, span.label))


def _deduped(windows: Sequence[ScoredWindow]) -> list[ScoredWindow]:
    """Time-ordered windows, exact duplicates collapsed."""
    seen: set[tuple[int, int]] = set()
    out = []
    for window in sorted(windows, key=lambda w: (w.start_ms, w.end_ms)):
        key = (window.start_ms, window.end_ms)
        if key in seen:
            continue
        seen.add(key)
        out.append(window)
    return out


def _spans_for(
    label: str,
    windows: Sequence[ScoredWindow],
    *,
    enter: float,
    sustain: float,
    bridge_gap_ms: int,
    min_windows: int,
) -> list[ClassSpan]:
    """One class's spans: open at ``enter``, hold while ``sustain`` does."""
    spans: list[ClassSpan] = []
    start = end = 0
    scores: list[float] = []

    def close() -> None:
        if len(scores) >= min_windows:
            spans.append(
                ClassSpan(
                    label=label,
                    start_ms=start,
                    end_ms=end,
                    windows=len(scores),
                    peak=max(scores),
                    mean=sum(scores) / len(scores),
                )
            )
        scores.clear()

    for window in windows:
        score = dict(window.labels).get(label, 0.0)
        # Close before open, so a merely-sustaining window after too long a gap
        # cannot reopen: reopening always costs `enter`.
        if scores and window.start_ms - end > bridge_gap_ms:
            close()
        if scores:
            if score >= sustain:
                scores.append(score)
                end = max(end, window.end_ms)
        elif score >= enter:
            start, end = window.start_ms, window.end_ms
            scores.append(score)
    close()
    return spans


def select_spans(
    spans: Sequence[ClassSpan], *, top_k: int, max_spans: int
) -> tuple[list[ClassSpan], list[ClassSummary]]:
    """The spans worth publishing, plus the map's ranked class summary.

    Classes rank by total covered time, and are kept whole — publishing half a
    class's spans leaves a lane with holes, which reads as "the sound stopped".
    """
    by_label: dict[str, list[ClassSpan]] = {}
    for span in spans:
        by_label.setdefault(span.label, []).append(span)
    summaries = [
        ClassSummary(
            label=label,
            spans=len(group),
            total_ms=sum(span.end_ms - span.start_ms for span in group),
            peak=max(span.peak for span in group),
        )
        for label, group in by_label.items()
    ]
    summaries.sort(key=lambda s: (-s.total_ms, -s.peak, s.label))

    kept: list[ClassSpan] = []
    ranked: list[ClassSummary] = []
    for summary in summaries[:top_k]:
        group = by_label[summary.label]
        if len(kept) + len(group) > max_spans:
            room = max_spans - len(kept)
            if room > 0 and not kept:
                longest = sorted(group, key=lambda s: s.start_ms - s.end_ms)[:room]
                kept += longest
                ranked.append(
                    ClassSummary(
                        label=summary.label,
                        spans=len(longest),
                        total_ms=sum(s.end_ms - s.start_ms for s in longest),
                        peak=max(s.peak for s in longest),
                    )
                )
            break
        kept += group
        ranked.append(summary)
    kept.sort(key=lambda span: (span.start_ms, span.end_ms, span.label))
    return kept, ranked


class SoundSpanPipeline(Pipeline):
    name = "sound-span"
    resource = Resource.AUDIO

    async def setup(self) -> None:
        self._settings = get_settings()

    async def discover(self, limit: int) -> Sequence[JobCreate]:
        async with DatabasePipe() as pipe:
            maps = await SoundSpanQueries(pipe.connection).maps_without_jobs(
                self.name, _SOURCE_PIPELINE, limit
            )
        return tuple(
            JobCreate(
                pipeline=self.name,
                session_id=tag_map.session_id,
                artifact_id=tag_map.id,
                dedup_key=f"{self.name}:artifact:{tag_map.id}",
            )
            for tag_map in maps
        )

    async def process(self, job: Job) -> dict[str, Any]:
        assert job.session_id is not None  # every job here comes from discover()
        if job.artifact_id is None:
            # Audio-tag republished; the new map carries its own job.
            return {"skipped": "audio-tag-map republished"}

        settings = self._settings
        async with DatabasePipe() as pipe:
            tag_map = await pipe.artifacts.get(job.artifact_id)
            rows = await SoundSpanQueries(pipe.connection).tag_windows(
                job.session_id, _SOURCE_PIPELINE, _MAX_WINDOWS
            )
        if tag_map is None:
            return {"skipped": "audio-tag-map vanished"}
        if len(rows) == _MAX_WINDOWS:
            logger.warning(
                "sound-span hit the %d window cap for session %s: spans cover "
                "only the start of it",
                _MAX_WINDOWS,
                job.session_id,
            )
        model = (tag_map.metadata.get("models") or {}).get("tagger")
        if tag_map.metadata.get("skipped"):
            return await self._publish(
                job,
                spans=[],
                classes=[],
                windows=0,
                model=model,
                skipped=str(tag_map.metadata["skipped"]),
            )

        windows = [
            window
            for row in rows
            if (window := read_window(row.start_ms, row.end_ms, row.metadata)) is not None
        ]
        spans = build_spans(
            windows,
            enter=settings.sound_span_enter_score,
            sustain=settings.sound_span_sustain_score,
            bridge_gap_ms=settings.sound_span_bridge_gap_ms,
            min_windows=settings.sound_span_min_windows,
        )
        kept, classes = select_spans(
            spans,
            top_k=settings.sound_span_top_k,
            max_spans=settings.sound_span_max_spans,
        )
        return await self._publish(
            job,
            spans=kept,
            classes=classes,
            windows=len(windows),
            model=model,
            truncated=len(kept) < len(spans),
        )

    async def _publish(
        self,
        job: Job,
        *,
        spans: list[ClassSpan],
        classes: list[ClassSummary],
        windows: int,
        model: str | None,
        truncated: bool = False,
        skipped: str | None = None,
    ) -> dict[str, Any]:
        """Replace the session's sound-span output atomically, map last-in-set."""
        if skipped:
            logger.info("sound-span skipping session %s: %s", job.session_id, skipped)
        settings = self._settings
        rows = [
            ArtifactCreate(
                pipeline=self.name,
                kind="sound-span",
                session_id=job.session_id,
                start_ms=span.start_ms,
                end_ms=span.end_ms,
                links={"audio_tag_map": str(job.artifact_id)},
                metadata={
                    "label": span.label,
                    "peak": span.peak,
                    "mean": span.mean,
                    "windows": span.windows,
                    "model": model,
                },
            )
            for span in spans
        ]
        map_metadata: dict[str, Any] = {
            "spans": len(spans),
            "windows": windows,
            "classes": [
                {
                    "label": summary.label,
                    "spans": summary.spans,
                    "total_ms": summary.total_ms,
                    "peak": summary.peak,
                }
                for summary in classes
            ],
            "model": model,
            "source_map": str(job.artifact_id),
            "params": {
                "enter": settings.sound_span_enter_score,
                "sustain": settings.sound_span_sustain_score,
                "bridge_gap_ms": settings.sound_span_bridge_gap_ms,
                "min_windows": settings.sound_span_min_windows,
                "top_k": settings.sound_span_top_k,
                "max_spans": settings.sound_span_max_spans,
            },
        }
        if truncated:
            map_metadata["truncated"] = True
        if skipped:
            map_metadata["skipped"] = skipped
        async with DatabasePipe() as pipe:
            if job.artifact_id is not None and await pipe.artifacts.get(job.artifact_id) is None:
                # Audio-tag republished mid-run; its new map has its own job.
                return {"skipped": "audio-tag-map republished mid-run"}
            await pipe.artifacts.delete_for_pipeline(job.session_id, self.name)
            await pipe.artifacts.create_many(rows)
            await pipe.artifacts.create(
                ArtifactCreate(
                    pipeline=self.name,
                    kind="sound-span-map",
                    session_id=job.session_id,
                    metadata=map_metadata,
                )
            )
        result: dict[str, Any] = {
            "spans": len(spans),
            "classes": len(classes),
            "windows": windows,
        }
        if skipped:
            result["skipped"] = skipped
        return result
