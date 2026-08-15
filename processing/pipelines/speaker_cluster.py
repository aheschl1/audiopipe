"""Tier: global speaker identity — batch-cluster voice embeddings.

Consumes each session's ``diarize-map`` (one job per session), but every run
re-clusters the user's **entire corpus** with constrained agglomerative
clustering (average linkage, cosine, one distance threshold): batch
re-clustering is order-independent and self-healing, where the old
attach-to-nearest-centroid design let one borderline assignment drag a
centroid between voices and snowball. Everything is scoped to (user,
embedding model) — cross-model embeddings are not comparable.

User curation persists as **constraints**, not state: ``speaker_pins`` rows
assert "this voice-print IS this identity". Pin-groups enter the
agglomeration pre-merged (must-link) and two clusters pinned to different
identities never merge (cannot-link), so manual merges and member moves
survive every rebuild. Cluster ids are otherwise ephemeral: after each run
result clusters are matched back to persistent ``speakers`` rows — pinned
identity first, else majority previous membership preferring named clusters
(named identities keep their id), else unnamed rows churn.

Assignments live in ``speaker_embeddings.speaker_id`` and cascade away with
diarize republication (embeddings regenerate under new artifact ids), but
pins are keyed on (session, label, model) and survive it; the fresh
diarize-map re-triggers clustering and the pins reclaim their identities.
Local labels on transcripts are never rewritten — global identity stays a
resolve-at-read mapping.

Batch runs are serialized per user by a transaction-scoped advisory lock:
the worker job, ``cli speakers recluster`` and ``POST /v1/speakers/recluster``
all funnel through :func:`rebuild_user_clusters`.
"""

import logging
from collections import Counter
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from database.pipe import DatabasePipe
from database.repos.pipelines.speaker_cluster import SpeakerClusterQueries
from database.schema.jobs import Job, JobCreate
from database.schema.speakers import CorpusEmbedding, SpeakerCreate
from processing.base import Pipeline

# Re-exported: the algorithm moved to processing.clustering so the diarize
# tier can reuse it (per-label purity audit) without a pipeline→pipeline
# import; existing importers keep working.
from processing.clustering import cluster_corpus  # noqa: F401
from processing.config import ProcessingSettings, get_settings
from resources import Resource

logger = logging.getLogger(__name__)

_SOURCE_PIPELINE = "diarize"


async def settings_for_user(
    pipe: DatabasePipe, user_id: Any, base: ProcessingSettings
) -> ProcessingSettings:
    """Layer the user's persisted overrides (NULL field = inherit) over env."""
    row = await pipe.cluster_params.get(user_id)
    if row is None:
        return base
    update: dict[str, Any] = {}
    if row.cluster_distance is not None:
        update["cluster_distance"] = row.cluster_distance
    if row.min_talk_ms is not None:
        update["cluster_min_talk_ms"] = row.min_talk_ms
    return base.model_copy(update=update) if update else base


async def lock_user_clustering(pipe: DatabasePipe, user_id: Any) -> None:
    """Serialize batch runs per user (worker vs CLI vs API) for the
    transaction's lifetime — two concurrent rebuilds would trample."""
    await pipe.connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s::text, 0))",
        (str(user_id),),
    )


async def rebuild_user_clusters(
    pipe: DatabasePipe,
    user_id: Any,
    base: ProcessingSettings,
    overrides: dict[str, Any] | None = None,
) -> dict[str, int]:
    """The one batch code path: lock → load → cluster per model → reconcile.

    Runs entirely on the caller's transaction. ``overrides`` (e.g. one-off
    CLI flags) layer on top of the user's persisted params, which layer on
    top of env defaults. Prints under the talk gate stay unassigned — unless
    pinned: a user assertion outranks the reliability heuristic.
    """
    await lock_user_clustering(pipe, user_id)
    effective = await settings_for_user(pipe, user_id, base)
    if overrides:
        effective = effective.model_copy(update=overrides)
    corpus = await pipe.speakers.corpus_for_user(user_id)

    counts = {"clusters": 0, "assigned": 0, "skipped_short": 0, "pinned": 0}
    by_model: dict[str, list[CorpusEmbedding]] = {}
    for row in corpus:
        by_model.setdefault(row.model, []).append(row)
    for model in sorted(by_model):
        rows = by_model[model]
        gated = [
            row
            for row in rows
            if row.pinned_to is not None
            or row.talk_ms is None
            or row.talk_ms >= effective.cluster_min_talk_ms
        ]
        counts["skipped_short"] += len(rows) - len(gated)
        counts["pinned"] += sum(1 for row in gated if row.pinned_to is not None)
        pins = {
            index: row.pinned_to
            for index, row in enumerate(gated)
            if row.pinned_to is not None
        }
        clusters = cluster_corpus(
            [row.embedding for row in gated], pins, effective.cluster_distance
        )
        counts["clusters"] += len(clusters)
        counts["assigned"] += sum(len(cluster) for cluster in clusters)
        await _reconcile(pipe, user_id, model, gated, clusters)
    logger.info("rebuilt clusters for user %s: %s", user_id, counts)
    return counts


async def _reconcile(
    pipe: DatabasePipe,
    user_id: Any,
    model: str,
    rows: Sequence[CorpusEmbedding],
    clusters: Sequence[Sequence[int]],
) -> None:
    """Map result clusters onto persistent ``speakers`` rows and write.

    Identity, in priority order: the cluster's pinned identity (unique — the
    clusterer pre-merged each pin-group and never merges across identities);
    else the *named* previous cluster most of its members came from; else the
    most common unnamed previous cluster (id stability); else a fresh unnamed
    row. Named clusters that end up with no members survive as identity
    anchors; unnamed empties are pruned.
    """
    existing = {
        speaker.id: speaker
        for speaker in await pipe.speakers.list_for_user_model(user_id, model)
    }
    claimed: set[UUID] = set()
    resolved: list[tuple[UUID | None, list[CorpusEmbedding]]] = []

    # Pins claim their identity before majority-matching can steal it.
    ordered = sorted(
        (tuple(cluster) for cluster in clusters),
        key=lambda cluster: (
            not any(rows[i].pinned_to is not None for i in cluster),
            -len(cluster),
            str(rows[cluster[0]].artifact_id),
        ),
    )
    for cluster in ordered:
        group = [rows[i] for i in cluster]
        pin_ids = {row.pinned_to for row in group if row.pinned_to is not None}
        if pin_ids:
            identity = pin_ids.pop()
            claimed.add(identity)
            resolved.append((identity, group))
        else:
            identity = _match_identity(group, existing, claimed)
            if identity is not None:
                claimed.add(identity)
            resolved.append((identity, group))

    await pipe.speakers.clear_assignments(user_id, model)
    for identity, group in resolved:
        if identity is None or identity not in existing:
            speaker = await pipe.speakers.create(
                SpeakerCreate(
                    user_id=user_id, model=model, centroid=group[0].embedding
                )
            )
            identity = speaker.id
        await pipe.speakers.set_assignments(
            identity, [row.artifact_id for row in group]
        )
        await pipe.speakers.recompute_centroid(identity)
    await pipe.speakers.delete_empty_unlabeled(user_id, model)


def _match_identity(
    group: Sequence[CorpusEmbedding],
    existing: dict[UUID, Any],
    claimed: set[UUID],
) -> UUID | None:
    previous = Counter(
        row.speaker_id for row in group if row.speaker_id is not None
    )
    ranked = sorted(previous.items(), key=lambda item: (-item[1], str(item[0])))
    for want_named in (True, False):
        for speaker_id, _count in ranked:
            speaker = existing.get(speaker_id)
            if speaker is None or speaker_id in claimed:
                continue
            if (speaker.name is not None) != want_named:
                continue
            return speaker_id
    return None


class SpeakerClusterPipeline(Pipeline):
    name = "speaker-cluster"
    resource = Resource.AUDIO

    async def setup(self) -> None:
        self._settings = get_settings()

    async def discover(self, limit: int) -> Sequence[JobCreate]:
        async with DatabasePipe() as pipe:
            maps = await SpeakerClusterQueries(pipe.connection).maps_without_jobs(
                self.name, _SOURCE_PIPELINE, limit
            )
        return tuple(
            JobCreate(
                pipeline=self.name,
                session_id=diarize_map.session_id,
                artifact_id=diarize_map.id,
                dedup_key=f"{self.name}:artifact:{diarize_map.id}",
            )
            for diarize_map in maps
        )

    async def process(self, job: Job) -> dict[str, Any]:
        assert job.session_id is not None
        if job.artifact_id is None:
            # Diarize republished; the new map has its own job.
            return {"skipped": "diarize-map republished"}
        async with DatabasePipe() as pipe:
            session = await pipe.sessions.get(job.session_id)
            if session is None:
                return {"skipped": "session vanished"}
            # A newer queued job for this user strictly supersedes this one:
            # skip before taking the per-user lock, so backfills do one
            # rebuild instead of N.
            if await SpeakerClusterQueries(pipe.connection).superseded(job):
                return {"skipped": "superseded by newer cluster job"}
            return await rebuild_user_clusters(
                pipe, session.user_id, self._settings
            )
