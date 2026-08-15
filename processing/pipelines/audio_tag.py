"""Tier: tag every session's audio with sound-event classes, plus embeddings.

Walks each ended session's audio in large spans, hands them to the
classification service (one ``audio_tagger`` container serving an AudioSet
tagger and a CLAP embedder — see ``processing/classifier.py``), and records
one ``audio-tag`` artifact per ~10s window: the window's top tag scores in
metadata and its CLAP embedding in ``audio_embeddings`` (pgvector), which is
what text->audio search scans. An ``audio-tag-map`` summary with the
session-level tags is written last as the completion marker.

Tag post-processing is deliberately in this tier, not the service: AudioSet's
ontology is a DAG whose parent classes co-fire with their children (a guitar
clip scores Music, Musical instrument and Guitar together), so windows drop
ancestors their descendants beat (``suppress_ancestors``). Session-level tags
take the per-class MAX across windows — sound events are sparse, a mean would
bury them — gated on persisting for ``min_consecutive`` windows to kill
one-window blips (``session_tags``).

Sessions whose audio cannot be placed on a timeline are recorded as skipped
rather than failed: retrying cannot help them.
"""

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from database.pipe import DatabasePipe
from database.repos.pipelines.common import PipelineDiscovery
from database.schema.artifacts import ArtifactCreate
from database.schema.embeddings import AudioEmbeddingCreate
from database.schema.jobs import Job, JobCreate
from processing.base import Pipeline
from processing.classifier import ClassifiedWindow, Classifier
from processing.config import get_settings
from resources import Resource
from services import stitch, timeline

logger = logging.getLogger(__name__)

# Direct-parent map of the AudioSet ontology (name -> parent names), generated
# from github.com/audioset/ontology. Data, not config: it changes only with
# the ontology itself.
_PARENTS_FILE = Path(__file__).resolve().parent.parent / "data" / "audioset_parents.json"


@dataclass(frozen=True, slots=True)
class TaggedWindow:
    """One classified window in session time, post ancestor-suppression."""

    start_ms: int
    end_ms: int
    labels: tuple[tuple[str, float], ...]
    embedding: tuple[float, ...]


def ancestors(label: str, parents: Mapping[str, Sequence[str]]) -> set[str]:
    """Every transitive ancestor of ``label`` in the ontology DAG."""
    seen: set[str] = set()
    frontier = list(parents.get(label, ()))
    while frontier:
        name = frontier.pop()
        if name in seen:
            continue
        seen.add(name)
        frontier += parents.get(name, ())
    return seen


def suppress_ancestors(
    labels: Sequence[tuple[str, float]], parents: Mapping[str, Sequence[str]]
) -> list[tuple[str, float]]:
    """Drop a label when a descendant of it scores at least as high: the more
    specific tag carries the information ("Car" beats "Vehicle")."""
    scores = dict(labels)
    dropped: set[str] = set()
    for label, score in labels:
        for ancestor in ancestors(label, parents):
            if ancestor in scores and scores[ancestor] <= score:
                dropped.add(ancestor)
    return [(label, score) for label, score in labels if label not in dropped]


def dedupe_windows(windows: Sequence[TaggedWindow]) -> list[TaggedWindow]:
    """Ordered windows with duplicate start times collapsed (span seams)."""
    seen: set[int] = set()
    out = []
    for window in sorted(windows, key=lambda w: (w.start_ms, w.end_ms)):
        if window.start_ms in seen:
            continue
        seen.add(window.start_ms)
        out.append(window)
    return out


def session_tags(
    windows: Sequence[TaggedWindow],
    *,
    threshold: float,
    min_consecutive: int,
    top_k: int,
) -> list[tuple[str, float]]:
    """The session's tag list: per-class MAX across windows, kept only when
    the class held ``threshold`` for ``min_consecutive`` windows in a row
    (capped at the window count, so short sessions can still tag)."""
    if not windows:
        return []
    required = min(min_consecutive, len(windows))
    peak: dict[str, float] = {}
    run: dict[str, int] = {}
    qualified: set[str] = set()
    for window in windows:
        above = {label: score for label, score in window.labels if score >= threshold}
        for label, score in above.items():
            peak[label] = max(peak.get(label, 0.0), score)
            run[label] = run.get(label, 0) + 1
            if run[label] >= required:
                qualified.add(label)
        for label in list(run):
            if label not in above:
                run[label] = 0
    ranked = sorted(
        ((label, peak[label]) for label in qualified), key=lambda t: (-t[1], t[0])
    )
    return ranked[:top_k]


def span_starts(total_ms: int, *, span_ms: int, overlap_ms: int) -> list[int]:
    """Start offsets of the rendered spans: consecutive spans overlap by one
    window-minus-hop so the service's hop grid stays continuous across the
    seam (its last full window of span N is the window before span N+1's
    first)."""
    step = max(1, span_ms - overlap_ms)
    starts = [0]
    while starts[-1] + span_ms < total_ms:
        starts.append(starts[-1] + step)
    return starts


class AudioTagPipeline(Pipeline):
    name = "audio-tag"
    resource = Resource.AUDIO

    async def setup(self) -> None:
        self._settings = get_settings()
        self._classifier = Classifier(self._settings)
        self._parents: dict[str, list[str]] = json.loads(_PARENTS_FILE.read_text())

    async def teardown(self) -> None:
        await self._classifier.close()

    async def discover(self, limit: int) -> Sequence[JobCreate]:
        async with DatabasePipe() as pipe:
            discovery = PipelineDiscovery(pipe.connection)
            sessions = await discovery.ended_sessions_with_resource_without(
                self.name, self.resource, limit
            )
        return tuple(
            JobCreate(
                pipeline=self.name,
                session_id=session.id,
                dedup_key=f"{self.name}:session:{session.id}",
            )
            for session in sessions
        )

    async def process(self, job: Job) -> dict[str, Any]:
        assert job.session_id is not None  # every job here comes from discover()
        settings = self._settings
        try:
            line = await timeline.load_timeline(job.session_id)
        except (stitch.NotStitchable, timeline.NotRenderable) as exc:
            return await self._publish(job, windows=[], skipped=str(exc))
        if line is None:
            return await self._publish(job, windows=[], skipped="session holds no audio")

        overlap_ms = settings.audio_tag_window_ms - settings.audio_tag_hop_ms
        windows: list[TaggedWindow] = []
        tagger_model = embedding_model = None
        # ClassifierError raises through: the worker retries with backoff.
        for start in span_starts(
            line.total_ms, span_ms=settings.audio_tag_span_ms, overlap_ms=overlap_ms
        ):
            end = min(start + settings.audio_tag_span_ms, line.total_ms)
            clip = await timeline.render_range(line, start, end)
            analysis = await self._classifier.analyze(
                clip, filename=f"{job.session_id}-{start}-{end}.wav"
            )
            tagger_model = analysis.tagger_model
            embedding_model = analysis.embedding_model
            windows += [self._absolute(start, window) for window in analysis.windows]

        return await self._publish(
            job,
            windows=dedupe_windows(windows),
            tagger_model=tagger_model,
            embedding_model=embedding_model,
        )

    def _absolute(self, span_start: int, window: ClassifiedWindow) -> TaggedWindow:
        """Clip-relative service window -> session time, labels cleaned."""
        kept = suppress_ancestors(
            [
                (label, score)
                for label, score in window.labels
                if score >= self._settings.audio_tag_window_min_score
            ],
            self._parents,
        )
        return TaggedWindow(
            start_ms=span_start + window.start_ms,
            end_ms=span_start + window.end_ms,
            labels=tuple(kept),
            embedding=window.embedding,
        )

    async def _publish(
        self,
        job: Job,
        *,
        windows: list[TaggedWindow],
        tagger_model: str | None = None,
        embedding_model: str | None = None,
        skipped: str | None = None,
    ) -> dict[str, Any]:
        """Replace the session's audio-tag output atomically, map last-in-set."""
        if skipped:
            logger.info("audio-tag skipping session %s: %s", job.session_id, skipped)
        settings = self._settings
        tags = session_tags(
            windows,
            threshold=settings.audio_tag_threshold,
            min_consecutive=settings.audio_tag_min_consecutive,
            top_k=settings.audio_tag_top_k,
        )
        rows = [
            ArtifactCreate(
                pipeline=self.name,
                kind="audio-tag",
                session_id=job.session_id,
                start_ms=window.start_ms,
                end_ms=window.end_ms,
                metadata={
                    "labels": [
                        {"label": label, "score": score} for label, score in window.labels
                    ],
                    "model": tagger_model,
                },
            )
            for window in windows
        ]
        map_metadata: dict[str, Any] = {
            "windows": len(windows),
            "tags": [{"label": label, "score": score} for label, score in tags],
            "models": {"tagger": tagger_model, "embedding": embedding_model},
            "params": {
                "threshold": settings.audio_tag_threshold,
                "min_consecutive": settings.audio_tag_min_consecutive,
                "top_k": settings.audio_tag_top_k,
                "window_min_score": settings.audio_tag_window_min_score,
            },
        }
        if skipped:
            map_metadata["skipped"] = skipped
        async with DatabasePipe() as pipe:
            await pipe.artifacts.delete_for_pipeline(job.session_id, self.name)
            created = await pipe.artifacts.create_many(rows)
            if created:
                await pipe.audio_embeddings.create_many(
                    [
                        AudioEmbeddingCreate(
                            artifact_id=artifact.id,
                            session_id=job.session_id,
                            model=embedding_model or "unknown",
                            embedding=window.embedding,
                        )
                        for artifact, window in zip(created, windows, strict=True)
                    ]
                )
            await pipe.artifacts.create(
                ArtifactCreate(
                    pipeline=self.name,
                    kind="audio-tag-map",
                    session_id=job.session_id,
                    metadata=map_metadata,
                )
            )
        result: dict[str, Any] = {"windows": len(windows), "tags": len(tags)}
        if skipped:
            result["skipped"] = skipped
        return result
