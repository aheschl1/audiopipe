"""Tier: split detected speech into per-speaker turns, with embeddings.

Consumes each session's ``speech-map`` (one diarize job per session), but
the diarizer never sees the whole session: speech-spans are re-merged into
*blocks* — contiguous speech regions — and one rendered clip per block goes
to the diarization service. Silence between blocks was already excluded by
the VAD tier and is never rendered or uploaded.

Blocks exist because diarization label consistency needs long context: within
one block "SPEAKER_00" is stable; across blocks it is not, so labels are
namespaced per block (``b{start}:SPEAKER_00``) and global identity is the
clustering tier's job — fed by the per-speaker voice-prints this tier stores
as pgvector rows (``speaker_embeddings``), queryable with distance operators.

The service also returns one embedding per *turn*, computed with overlapping
speech masked out. Those power the **purity audit**
(:func:`split_labels`): the diarizer's internal clustering sometimes puts several
people under one label, and its per-label aggregate — a blend of their
voices — cannot reveal that. When a label's own turn vectors form clearly
separated groups, the label splits into sub-labels (``SPEAKER_10.0``, ``.1``)
*before* anything is published, so utterances, transcripts, and voice-prints
all derive from the corrected labels and nothing downstream changes shape.
Voice-prints are the clean-turn-weighted mean per final label (crosstalk
frames excluded), with the service aggregate as fallback; false splits are
recoverable with the existing merge/pin tools, unlike a blended print, which
is why the audit errs toward splitting. Per-turn vectors are deliberately
ephemeral — republication recomputes them; INFO logs carry the split
diagnostics for threshold calibration.

Besides turns, the tier publishes ``utterance`` artifacts — same-speaker
turns merged into ASR-sized units — which are what the transcribe tier
consumes. That makes this tier the transcription gate: the diarizer's
segmentation hears far-field speakers the VAD misses, so gating ASR on
anything less sensitive silences one side of a conversation. Merges refuse
to span another speaker's interjection beyond a small crosstalk budget (the
whole rendered span goes to ASR, so a spanned interjection would land in the
wrong transcript). Overlap is metadata, never a gate: turns, utterances, and
(downstream) transcripts carry ``overlap_ms`` so crosstalk is queryable, but
overlapped speech is always still transcribed.

Publication is atomic: previous diarize output for the session is deleted and
the full new set (turns, utterances, embeddings, the ``diarize-map`` summary)
inserted in one transaction — along with the session's now-stale transcripts,
which this tier owns invalidating: they derive from utterances that no longer
exist. The map's presence is the completion marker consumers wait for, so
partial output is never visible and retries are idempotent.
"""

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

import numpy as np

from database.pipe import DatabasePipe
from database.repos.pipelines.diarize import DiarizeQueries
from database.schema.artifacts import ArtifactCreate
from database.schema.embeddings import SpeakerEmbeddingCreate
from database.schema.jobs import Job, JobCreate
from processing.base import Pipeline
from processing.clustering import cluster_corpus
from processing.config import get_settings
from processing.diarizer import Diarizer, Turn
from resources import Resource
from services import stitch, timeline

logger = logging.getLogger(__name__)

_SOURCE_PIPELINE = "speech-detect"


@dataclass(frozen=True, slots=True)
class Block:
    start_ms: int
    end_ms: int


def blocks_from_spans(
    spans: Sequence[tuple[int, int]], *, merge_gap_ms: int, max_block_ms: int
) -> list[Block]:
    """Merge ordered speech spans into bounded contiguous blocks.

    Adjacent spans join when the silence between them is at most
    ``merge_gap_ms``; a block that would exceed ``max_block_ms`` is closed at
    the previous span boundary instead (never mid-span — spans are short, and
    a span boundary is a place the VAD already called quiet).
    """
    blocks: list[Block] = []
    for start, end in spans:
        if (
            blocks
            and start - blocks[-1].end_ms <= merge_gap_ms
            and end - blocks[-1].start_ms <= max_block_ms
        ):
            blocks[-1] = Block(blocks[-1].start_ms, max(blocks[-1].end_ms, end))
        else:
            blocks.append(Block(start, end))
    return blocks


@dataclass(frozen=True, slots=True)
class Utterance:
    start_ms: int
    end_ms: int
    speaker: str  # block-namespaced label
    turns: int  # source turns merged in


def _gap_crosstalk_ms(
    others: Sequence[tuple[int, int]], gap_start: int, gap_end: int
) -> int:
    """Total time within [gap_start, gap_end) covered by ``others`` (sorted
    by start; union, not sum — stacked overlaps don't double-count)."""
    if gap_end <= gap_start:
        return 0
    total, cursor = 0, gap_start
    for start, end in others:
        if start >= gap_end:
            break
        clipped_start, clipped_end = max(start, cursor), min(end, gap_end)
        if clipped_end > clipped_start:
            total += clipped_end - clipped_start
            cursor = clipped_end
    return total


def utterances_from_turns(
    turns: Sequence[tuple[int, int, str]],
    *,
    merge_gap_ms: int,
    max_utterance_ms: int,
    crosstalk_max_ms: int | None = None,
    crosstalk_turns: Sequence[tuple[int, int, str]] | None = None,
) -> list[Utterance]:
    """Merge same-speaker turns into ASR-sized utterances.

    Merging is per speaker label: block namespacing already prevents
    cross-block joins. Turns are sorted first (service order is not
    trusted); zero-length turns are dropped; overlapping same-speaker turns
    (segmentation artifacts) collapse. A merge that would exceed
    ``max_utterance_ms`` closes at the previous turn boundary instead; a
    single turn longer than the cap is split into near-equal pieces (never a
    sliver tail).

    A merge may span another speaker's *short* interruption — a backchannel
    shouldn't split a sentence — but not a real interjection: ASR
    transcribes the whole rendered span, so foreign speech inside a merge
    gap lands in this speaker's transcript. When ``crosstalk_max_ms`` is
    set, a merge is refused if other speakers' turns cover more than that
    much of the gap. Gating reads ``crosstalk_turns`` — pass the *raw*
    unfiltered turn list there: a sub-``min_turn_ms`` interjection carries
    real foreign speech even though it is too short to keep as an artifact
    (defaults to ``turns`` itself). ``None`` preserves the historical
    unconditional-merge behavior.
    """
    by_speaker: dict[str, list[tuple[int, int]]] = {}
    for start, end, speaker in turns:
        if end > start:
            by_speaker.setdefault(speaker, []).append((start, end))

    gating_source = turns if crosstalk_turns is None else crosstalk_turns
    out: list[Utterance] = []
    for speaker, speaker_turns in by_speaker.items():
        if crosstalk_max_ms is None:
            others: list[tuple[int, int]] = []
        else:
            others = sorted(
                (start, end)
                for start, end, other in gating_source
                if other != speaker and end > start
            )
        merged: list[list[int]] = []  # [start, end, turn count]
        for start, end in sorted(speaker_turns):
            if (
                merged
                and start - merged[-1][1] <= merge_gap_ms
                and max(end, merged[-1][1]) - merged[-1][0] <= max_utterance_ms
                and (
                    crosstalk_max_ms is None
                    or _gap_crosstalk_ms(others, merged[-1][1], start)
                    <= crosstalk_max_ms
                )
            ):
                merged[-1][1] = max(merged[-1][1], end)
                merged[-1][2] += 1
            else:
                merged.append([start, end, 1])
        for start, end, count in merged:
            duration = end - start
            if duration <= max_utterance_ms:
                out.append(Utterance(start, end, speaker, count))
                continue
            # Only a single over-cap turn reaches here (merges respect the cap).
            pieces = math.ceil(duration / max_utterance_ms)
            for index in range(pieces):
                out.append(
                    Utterance(
                        start + duration * index // pieces,
                        start + duration * (index + 1) // pieces,
                        speaker,
                        count,
                    )
                )
    out.sort(key=lambda u: (u.start_ms, u.end_ms, u.speaker))
    return out


def _unit_centroid(vectors: Sequence[Sequence[float]]) -> np.ndarray:
    """Mean of L2-normalized vectors, re-normalized (cosine-space centroid)."""
    matrix = np.asarray(vectors, dtype=float)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    mean = (matrix / norms).mean(axis=0)
    length = float(np.linalg.norm(mean))
    return mean / length if length > 0.0 else mean


def split_labels(
    turns: Sequence[Turn],
    *,
    distance: float,
    split_min_clean_ms: int,
    turn_min_clean_ms: int,
    log_context: str = "",
) -> list[str]:
    """The purity audit: a final label per turn, splitting mixed ones.

    The diarizer's internal clustering can wrongly put several people under one
    label, and its per-label aggregate embedding (a blend of their voices)
    cannot reveal that. Per-turn embeddings can: each label's turn vectors
    are clustered locally (same agglomeration as the global tier, no pins),
    and when they form ≥2 well-separated groups each carrying at least
    ``split_min_clean_ms`` of clean speech, the label splits into sub-labels
    (``SPEAKER_10.0``, ``.1``, … — a grammar the service's ``SPEAKER_\\d+``
    can't collide with), numbered by descending clean talk.

    Only turns with an embedding from at least ``turn_min_clean_ms`` of
    clean audio vote. Below-floor groups fold into the nearest surviving
    group by centroid cosine distance — turns must keep coverage, they gate
    transcription. Voteless turns attach to the group with the temporally
    nearest voting turn (tie → the biggest group). A label whose vectors
    form one group — or that lacks the votes to judge — keeps its plain
    name. Returns final labels parallel to ``turns``.
    """
    final = [turn.speaker for turn in turns]
    by_label: dict[str, list[int]] = {}
    for index, turn in enumerate(turns):
        by_label.setdefault(turn.speaker, []).append(index)

    for label, indices in sorted(by_label.items()):
        voters = [
            i
            for i in indices
            if turns[i].embedding is not None
            and (turns[i].clean_ms or 0) >= turn_min_clean_ms
        ]
        if len(voters) < 2:
            continue
        clusters = cluster_corpus(
            [turns[i].embedding for i in voters], {}, distance
        )
        if len(clusters) < 2:
            continue

        def clean_sum(cluster: Sequence[int]) -> int:
            return sum(turns[voters[j]].clean_ms or 0 for j in cluster)

        surviving = sorted(
            (cluster for cluster in clusters if clean_sum(cluster) >= split_min_clean_ms),
            key=lambda cluster: (-clean_sum(cluster), cluster[0]),
        )
        if len(surviving) < 2:
            continue

        centroids = [
            _unit_centroid([turns[voters[j]].embedding for j in cluster])
            for cluster in surviving
        ]
        # Sub-group index per turn index; below-floor groups fold into the
        # nearest surviving group so no turn is ever dropped.
        group_of: dict[int, int] = {}
        for group, cluster in enumerate(surviving):
            for j in cluster:
                group_of[voters[j]] = group
        for cluster in clusters:
            if clean_sum(cluster) >= split_min_clean_ms:
                continue
            centroid = _unit_centroid([turns[voters[j]].embedding for j in cluster])
            nearest = min(
                range(len(centroids)),
                key=lambda g: (1.0 - float(np.dot(centroid, centroids[g])), g),
            )
            for j in cluster:
                group_of[voters[j]] = nearest

        def interval_gap(a: Turn, b: Turn) -> int:
            return max(a.start_ms - b.end_ms, b.start_ms - a.end_ms, 0)

        assigned = dict(group_of)
        for i in indices:
            if i in assigned:
                continue
            assigned[i] = min(
                set(group_of.values()),
                key=lambda g: (
                    min(
                        interval_gap(turns[i], turns[v])
                        for v, gv in group_of.items()
                        if gv == g
                    ),
                    g,
                ),
            )
        for i in indices:
            final[i] = f"{label}.{assigned[i]}"

        pairwise = [
            round(1.0 - float(np.dot(centroids[a], centroids[b])), 3)
            for a in range(len(centroids))
            for b in range(a + 1, len(centroids))
        ]
        logger.info(
            "%ssplit %s into %d sub-labels (clean talk %s ms, centroid distances %s)",
            f"{log_context}: " if log_context else "",
            label,
            len(surviving),
            [clean_sum(cluster) for cluster in surviving],
            pairwise,
        )
    return final


class DiarizePipeline(Pipeline):
    name = "diarize"
    resource = Resource.AUDIO

    async def setup(self) -> None:
        self._settings = get_settings()
        self._diarizer = Diarizer(self._settings)

    async def teardown(self) -> None:
        await self._diarizer.close()

    async def discover(self, limit: int) -> Sequence[JobCreate]:
        async with DatabasePipe() as pipe:
            maps = await DiarizeQueries(pipe.connection).maps_without_jobs(
                self.name, _SOURCE_PIPELINE, limit
            )
        return tuple(
            JobCreate(
                pipeline=self.name,
                session_id=speech_map.session_id,
                artifact_id=speech_map.id,
                dedup_key=f"{self.name}:artifact:{speech_map.id}",
            )
            for speech_map in maps
        )

    async def process(self, job: Job) -> dict[str, Any]:
        assert job.session_id is not None and job.artifact_id is not None
        settings = self._settings

        async with DatabasePipe() as pipe:
            speech_map = await pipe.artifacts.get(job.artifact_id)
            spans = await pipe.artifacts.list_for_session(
                job.session_id, pipeline=_SOURCE_PIPELINE, kind="speech-span",
                limit=1_000_000,
            )
        if speech_map is None:
            return {"skipped": "speech-map vanished"}
        if not spans or speech_map.metadata.get("skipped"):
            return await self._publish(
                job, turns=[], utterances=[], embeddings=[], blocks=0
            )

        blocks = blocks_from_spans(
            [(span.start_ms, span.end_ms) for span in spans],
            merge_gap_ms=settings.diarize_block_merge_gap_ms,
            max_block_ms=settings.diarize_max_block_ms,
        )
        try:
            line = await timeline.load_timeline(job.session_id)
            if line is None:
                return await self._publish(
                    job,
                    turns=[],
                    utterances=[],
                    embeddings=[],
                    blocks=0,
                    skipped="session audio is gone",
                )
            # DiarizerError raises through: the worker retries with backoff.
            turn_rows, embedding_rows, raw_turns = [], [], []
            for block in blocks:
                clip = await timeline.render_range(line, block.start_ms, block.end_ms)
                result = await self._diarizer.diarize(
                    clip, filename=f"{job.session_id}-{block.start_ms}.wav"
                )
                # The purity audit: relabel turns of any diarizer label whose
                # own turn embeddings prove it contains several people.
                labels = split_labels(
                    result.turns,
                    distance=settings.diarize_split_distance,
                    split_min_clean_ms=settings.diarize_split_min_clean_ms,
                    turn_min_clean_ms=settings.diarize_turn_min_clean_ms,
                    log_context=f"session {job.session_id} block {block.start_ms}",
                )
                final_turns = [
                    replace(turn, speaker=label)
                    for turn, label in zip(result.turns, labels, strict=True)
                ]
                turn_rows += self._turn_artifacts(job, block, final_turns)
                embedding_rows += self._embedding_rows(job, block, result, final_turns)
                # Unfiltered, for merge gating: sub-minimum interjections
                # carry real foreign speech.
                raw_turns += [
                    (
                        block.start_ms + turn.start_ms,
                        block.start_ms + turn.end_ms,
                        f"b{block.start_ms}:{turn.speaker}",
                    )
                    for turn in final_turns
                ]
        except (stitch.NotStitchable, timeline.NotRenderable) as exc:
            # Retrying cannot make the audio renderable.
            return await self._publish(
                job, turns=[], utterances=[], embeddings=[], blocks=0, skipped=str(exc)
            )

        return await self._publish(
            job,
            turns=turn_rows,
            utterances=self._utterance_artifacts(job, turn_rows, raw_turns),
            embeddings=embedding_rows,
            blocks=len(blocks),
        )

    def _turn_artifacts(
        self, job: Job, block: Block, turns: Sequence[Turn]
    ) -> list[ArtifactCreate]:
        minimum = self._settings.diarize_min_turn_ms
        rows = []
        for turn in turns:
            if turn.end_ms - turn.start_ms < minimum:
                continue
            rows.append(
                ArtifactCreate(
                    pipeline=self.name,
                    kind="speaker-turn",
                    session_id=job.session_id,
                    start_ms=block.start_ms + turn.start_ms,
                    end_ms=block.start_ms + turn.end_ms,
                    metadata={
                        "speaker": f"b{block.start_ms}:{turn.speaker}",
                        "block_start_ms": block.start_ms,
                        "block_end_ms": block.end_ms,
                        # Time shared with other speakers' turns: crosstalk
                        # provenance for consumers (0 when the service
                        # cannot say).
                        "overlap_ms": turn.overlap_ms,
                    },
                )
            )
        return rows

    def _utterance_artifacts(
        self,
        job: Job,
        turn_rows: Sequence[ArtifactCreate],
        raw_turns: Sequence[tuple[int, int, str]],
    ) -> list[ArtifactCreate]:
        """ASR units from the already-filtered turn rows: what transcribe eats.

        ``raw_turns`` (unfiltered) feeds the crosstalk merge gate. Each
        utterance carries ``overlap_ms`` — its turns' overlap prorated by
        how much of each turn falls inside the utterance span (over-cap
        turns are split into pieces) — so "this transcript contains
        overlapped speech" stays queryable downstream.
        """
        settings = self._settings
        blocks_by_speaker = {
            row.metadata["speaker"]: (
                row.metadata["block_start_ms"],
                row.metadata["block_end_ms"],
            )
            for row in turn_rows
        }
        rows_by_speaker: dict[str, list[tuple[int, int, int]]] = {}
        for row in turn_rows:
            rows_by_speaker.setdefault(row.metadata["speaker"], []).append(
                (row.start_ms, row.end_ms, row.metadata["overlap_ms"])
            )
        utterances = utterances_from_turns(
            [(row.start_ms, row.end_ms, row.metadata["speaker"]) for row in turn_rows],
            merge_gap_ms=settings.diarize_utterance_merge_gap_ms,
            max_utterance_ms=settings.diarize_max_utterance_ms,
            crosstalk_max_ms=settings.diarize_merge_crosstalk_max_ms,
            crosstalk_turns=raw_turns,
        )
        rows = []
        for utterance in utterances:
            block_start, block_end = blocks_by_speaker[utterance.speaker]
            overlap_ms = 0
            for start, end, turn_overlap in rows_by_speaker[utterance.speaker]:
                covered = min(end, utterance.end_ms) - max(start, utterance.start_ms)
                if covered > 0:
                    overlap_ms += round(turn_overlap * covered / (end - start))
            rows.append(
                ArtifactCreate(
                    pipeline=self.name,
                    kind="utterance",
                    session_id=job.session_id,
                    start_ms=utterance.start_ms,
                    end_ms=utterance.end_ms,
                    metadata={
                        "speaker": utterance.speaker,
                        "turns": utterance.turns,
                        "block_start_ms": block_start,
                        "block_end_ms": block_end,
                        "overlap_ms": overlap_ms,
                    },
                )
            )
        return rows

    def _embedding_rows(
        self, job: Job, block: Block, result: Any, turns: Sequence[Turn]
    ) -> list[tuple[ArtifactCreate, list[float]]]:
        """One artifact plus its vector per (block, final label) voice-print.

        The vector lands in ``speaker_embeddings`` (pgvector), cascade-keyed
        by the artifact row, so the delete-then-recreate publication covers
        vectors and rows in the same transaction; the artifact carries the
        addressing.

        ``turns`` carry the post-audit final labels; ``result`` still holds
        the service's per-original-label aggregates. The voice-print is the
        clean_ms-weighted mean of the label's clean turn embeddings —
        crosstalk frames never enter it — falling back to the service
        aggregate for labels with no usable turn vectors (old service, or
        all-overlapped speech: exactly the print the clean-talk gate should
        then skip). ``clean_talk_ms`` is only written for labels whose turns
        reported clean time, so a degraded or partially-degraded response
        makes the clustering gate fall back to ``talk_ms`` per label instead
        of gating whole speakers out.
        """
        settings = self._settings
        talk_ms: dict[str, int] = {}
        clean_talk_ms: dict[str, int] = {}
        voters: dict[str, list[Turn]] = {}
        original_label: dict[str, str] = {}
        clean_known: dict[str, bool] = {}
        for original, turn in zip(result.turns, turns, strict=True):
            label = turn.speaker
            talk_ms[label] = talk_ms.get(label, 0) + (turn.end_ms - turn.start_ms)
            clean_talk_ms[label] = clean_talk_ms.get(label, 0) + (turn.clean_ms or 0)
            clean_known[label] = clean_known.get(label, False) or (
                turn.clean_ms is not None
            )
            original_label[label] = original.speaker
            if (
                turn.embedding is not None
                and (turn.clean_ms or 0) >= settings.diarize_turn_min_clean_ms
            ):
                voters.setdefault(label, []).append(turn)

        rows = []
        for label in sorted(talk_ms):
            vector: list[float] | None = None
            label_voters = voters.get(label)
            if label_voters:
                weights = np.asarray(
                    [max(turn.clean_ms or 0, 1) for turn in label_voters], dtype=float
                )
                matrix = np.asarray(
                    [turn.embedding for turn in label_voters], dtype=float
                )
                norms = np.linalg.norm(matrix, axis=1, keepdims=True)
                norms[norms == 0.0] = 1.0
                pooled = ((matrix / norms) * weights[:, None]).sum(axis=0)
                length = float(np.linalg.norm(pooled))
                if length > 0.0:
                    vector = [float(value) for value in pooled / length]
            if vector is None:
                vector = result.embeddings.get(original_label[label])
            if vector is None:
                # Aggregate was dropped (NaN) and nothing clean to pool: the
                # label keeps its turns but gets no voice-print, as before.
                continue
            metadata: dict[str, Any] = {
                "speaker": f"b{block.start_ms}:{label}",
                "dim": len(vector),
                "model": result.model,
                # The clustering tier's quality gate: voice-prints from very
                # little speech are noise.
                "talk_ms": talk_ms[label],
            }
            if clean_known[label]:
                metadata["clean_talk_ms"] = clean_talk_ms[label]
            if original_label[label] != label:
                metadata["split_of"] = original_label[label]
            rows.append(
                (
                    ArtifactCreate(
                        pipeline=self.name,
                        kind="speaker-embedding",
                        session_id=job.session_id,
                        start_ms=block.start_ms,
                        end_ms=block.end_ms,
                        metadata=metadata,
                    ),
                    vector,
                )
            )
        return rows

    async def _publish(
        self,
        job: Job,
        *,
        turns: list[ArtifactCreate],
        utterances: list[ArtifactCreate],
        embeddings: list[tuple[ArtifactCreate, list[float]]],
        blocks: int,
        skipped: str | None = None,
    ) -> dict[str, Any]:
        """Replace the session's diarize output atomically, map last-in-set."""
        if skipped:
            logger.info("diarize skipping session %s: %s", job.session_id, skipped)
        settings = self._settings
        map_metadata: dict[str, Any] = {
            "blocks": blocks,
            "turns": len(turns),
            "utterances": len(utterances),
            "speakers": len(embeddings),
            "params": {
                "block_merge_gap_ms": settings.diarize_block_merge_gap_ms,
                "max_block_ms": settings.diarize_max_block_ms,
                "min_turn_ms": settings.diarize_min_turn_ms,
                "utterance_merge_gap_ms": settings.diarize_utterance_merge_gap_ms,
                "max_utterance_ms": settings.diarize_max_utterance_ms,
                "merge_crosstalk_max_ms": settings.diarize_merge_crosstalk_max_ms,
                "split_distance": settings.diarize_split_distance,
                "split_min_clean_ms": settings.diarize_split_min_clean_ms,
                "turn_min_clean_ms": settings.diarize_turn_min_clean_ms,
            },
        }
        if skipped:
            map_metadata["skipped"] = skipped
        async with DatabasePipe() as pipe:
            await pipe.artifacts.delete_for_pipeline(job.session_id, self.name)
            # Transcripts derive from utterances this delete just invalidated;
            # leaving them would show stale generations on the timeline, so
            # this tier owns removing them. Transcribe jobs whose utterance
            # vanished skip themselves (artifact_id goes NULL).
            await pipe.artifacts.delete_for_pipeline(job.session_id, "transcribe")
            await pipe.artifacts.create_many(turns)
            await pipe.artifacts.create_many(utterances)
            created = await pipe.artifacts.create_many([row for row, _ in embeddings])
            await pipe.embeddings.create_many(
                [
                    SpeakerEmbeddingCreate(
                        artifact_id=artifact.id,
                        session_id=job.session_id,
                        speaker=artifact.metadata["speaker"],
                        model=artifact.metadata["model"],
                        embedding=vector,
                    )
                    for artifact, (_, vector) in zip(created, embeddings, strict=True)
                ]
            )
            await pipe.artifacts.create(
                ArtifactCreate(
                    pipeline=self.name,
                    kind="diarize-map",
                    session_id=job.session_id,
                    metadata=map_metadata,
                )
            )
        result = {
            "blocks": blocks,
            "turns": len(turns),
            "utterances": len(utterances),
            "speakers": len(embeddings),
        }
        if skipped:
            result["skipped"] = skipped
        return result
