"""Tier: A/B transcript candidates — 2 models x 2 strategies per utterance.

Chained-only (discover returns nothing): `POST /v1/sessions/{id}/ab` inserts
the job directly. One run republishes the whole candidate set for the
session: for every diarized utterance it stores four `transcript-candidate`
artifacts — parakeet and whisper, each transcribed two ways:

- ``chunk``  — the utterance clip alone, exactly like the production tier.
- ``block``  — the whole diarize block transcribed once with word
  timestamps, words rebased to session time and assigned to utterances by
  midpoint. This is the strategy that gives 30s-context models (whisper) a
  fair shot; a word under crosstalk lands in every utterance whose span
  covers it, mirroring what the chunk clip's audio contains.

Candidates never touch the canonical transcript: the voting route promotes
a winner. A model/strategy failure costs that candidate, not the job — but
a run that produced nothing while erroring raises so the worker retries.
"""

from collections.abc import Sequence
from typing import Any

from database.pipe import DatabasePipe
from database.schema.artifacts import ArtifactCreate, PipelineArtifact
from database.schema.jobs import Job, JobCreate
from processing.base import Pipeline
from processing.config import get_settings
from processing.transcriber import Transcriber, TranscriberError
from resources import Resource
from services import stitch, timeline

MODELS = ("parakeet", "whisper")
# Parakeet's comfortable long-form ceiling; blocks cap at 30 min.
BLOCK_RENDER_CAP_MS = 1_200_000


def sub_spans(start_ms: int, end_ms: int, cap_ms: int = BLOCK_RENDER_CAP_MS) -> list[tuple[int, int]]:
    spans = []
    cursor = start_ms
    while cursor < end_ms:
        spans.append((cursor, min(cursor + cap_ms, end_ms)))
        cursor += cap_ms
    return spans


def assign_words(
    words: Sequence[dict], utterances: Sequence[PipelineArtifact]
) -> dict[Any, list[dict]]:
    """Session-absolute words -> utterance ids, by word midpoint. Overlapping
    utterance spans (crosstalk) each receive the word."""
    out: dict[Any, list[dict]] = {u.id: [] for u in utterances}
    for word in words:
        mid = (word["s"] + word["e"]) // 2
        for u in utterances:
            if u.start_ms <= mid < u.end_ms:
                out[u.id].append(word)
    return out


def _blocks(utterances: Sequence[PipelineArtifact]) -> dict[tuple[int, int], list[PipelineArtifact]]:
    groups: dict[tuple[int, int], list[PipelineArtifact]] = {}
    for u in utterances:
        start = u.metadata.get("block_start_ms")
        end = u.metadata.get("block_end_ms")
        # Pre-block-era utterances degrade to a block of their own span.
        span = (start, end) if isinstance(start, int) and isinstance(end, int) else (u.start_ms, u.end_ms)
        groups.setdefault(span, []).append(u)
    return groups


class TranscribeAbPipeline(Pipeline):
    name = "transcribe-ab"
    resource = Resource.AUDIO

    async def setup(self) -> None:
        self._settings = get_settings()
        self._transcriber = Transcriber(self._settings)

    async def teardown(self) -> None:
        await self._transcriber.close()

    async def discover(self, limit: int) -> Sequence[JobCreate]:
        return ()

    async def process(self, job: Job) -> dict[str, Any]:
        assert job.session_id is not None
        async with DatabasePipe() as pipe:
            utterances = await pipe.artifacts.list_for_session(
                job.session_id, pipeline="diarize", kind="utterance", limit=1_000_000
            )
        if not utterances:
            return {"skipped": "no utterances to compare"}

        try:
            line = await timeline.load_timeline(job.session_id)
            if line is None:
                return {"skipped": "session audio is gone"}
        except stitch.NotStitchable as exc:
            return {"skipped": str(exc)}

        # Republication up front, then INCREMENTAL inserts: candidates appear
        # as they're produced, which is what lets the UI show live progress.
        # A retried run re-deletes and starts over.
        async with DatabasePipe() as pipe:
            await pipe.artifacts.delete_for_pipeline(job.session_id, self.name)

        errors = 0
        produced = 0

        async def publish(batch: list[ArtifactCreate]) -> None:
            nonlocal produced
            if not batch:
                return
            async with DatabasePipe() as pipe:
                await pipe.artifacts.create_many(batch)
            produced += len(batch)

        for u in utterances:
            try:
                clip = await timeline.render_range(line, u.start_ms, u.end_ms)
            except timeline.NotRenderable:
                continue
            batch: list[ArtifactCreate] = []
            for model in MODELS:
                try:
                    result = await self._transcriber.transcribe(
                        clip,
                        filename=f"{job.session_id}-{u.start_ms}-{u.end_ms}.wav",
                        model=model,
                    )
                except TranscriberError:
                    errors += 1
                    continue
                words = [
                    {"w": w.word, "s": u.start_ms + w.start_ms, "e": u.start_ms + w.end_ms}
                    for w in (result.words or ())
                ]
                batch.append(
                    self._candidate(job, u, model, "chunk", result.text, words, result.language)
                )
            await publish(batch)

        blocks = _blocks(utterances)
        for (block_start, block_end), members in blocks.items():
            for model in MODELS:
                words: list[dict] = []
                language = None
                failed = False
                for span_start, span_end in sub_spans(block_start, block_end):
                    try:
                        audio = await timeline.render_range(line, span_start, span_end)
                        result = await self._transcriber.transcribe(
                            audio,
                            filename=f"{job.session_id}-{span_start}-{span_end}.wav",
                            model=model,
                        )
                    except (timeline.NotRenderable, TranscriberError):
                        errors += 1
                        failed = True
                        break
                    words.extend(
                        {"w": w.word, "s": span_start + w.start_ms, "e": span_start + w.end_ms}
                        for w in (result.words or ())
                    )
                    language = result.language or language
                if failed:
                    continue
                assigned = assign_words(words, members)
                await publish(
                    [
                        self._candidate(
                            job, u, model, "block",
                            " ".join(w["w"] for w in assigned[u.id]),
                            assigned[u.id], language,
                        )
                        for u in members
                    ]
                )

        if not produced and errors:
            raise TranscriberError(f"no candidates produced ({errors} errors)")

        return {
            "utterances": len(utterances),
            "candidates": produced,
            "blocks": len(blocks),
            "errors": errors,
        }

    def _candidate(
        self,
        job: Job,
        utterance: PipelineArtifact,
        model: str,
        strategy: str,
        text: str,
        words: list[dict],
        language: str | None,
    ) -> ArtifactCreate:
        metadata: dict[str, Any] = {
            "text": text,
            "chars": len(text),
            "model": model,
            "strategy": strategy,
            "speaker": utterance.metadata.get("speaker"),
            "words": words,
        }
        if language:
            metadata["language"] = language
        return ArtifactCreate(
            pipeline=self.name,
            kind="transcript-candidate",
            session_id=job.session_id,
            start_ms=utterance.start_ms,
            end_ms=utterance.end_ms,
            links={"utterance": str(utterance.id)},
            metadata=metadata,
        )
