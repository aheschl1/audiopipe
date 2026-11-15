"""Tier: group a session's utterances into conversations by inter-turn gap.

A conversation closes when the silence between one utterance's end and the
next's start exceeds ``gap_ms``, and never spans a diarization block, so its
labels stay consistent; a second pass then splits it where the participants
change. A lone utterance is never a conversation. Every boundary records why
it was placed, so a later adjudicator (an LLM) can revisit it.
"""

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from database.pipe import DatabasePipe
from database.repos.pipelines.conversation import ConversationQueries
from database.schema.artifacts import ArtifactCreate
from database.schema.jobs import Job, JobCreate
from processing.base import Pipeline
from processing.config import get_settings
from resources import Resource

logger = logging.getLogger(__name__)

_SOURCE_PIPELINE = "diarize"

# Utterances are ≥300ms each; this is days of nonstop speech.
_MAX_UTTERANCES = 200_000

Boundary = Literal["gap", "block", "speaker_change", "session_start", "session_end"]


@dataclass(frozen=True, slots=True)
class Turn:
    """One upstream ``utterance`` artifact, reduced to what grouping needs."""

    id: UUID
    start_ms: int
    end_ms: int
    speaker: str | None
    block_start_ms: int | None


@dataclass(frozen=True, slots=True)
class Exclusion:
    """One utterance removed from conversation building, and why."""

    utterance_id: UUID
    reason: str | None
    source: str


@dataclass(frozen=True, slots=True)
class Stats:
    """What a set of member turns adds up to."""

    start_ms: int
    end_ms: int
    turns: int
    alternations: int
    speakers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Conversation:
    members: tuple[Turn, ...]
    stats: Stats
    opening: Boundary
    closure: Boundary
    gap_before_ms: int | None
    gap_after_ms: int | None


def read_turn(
    artifact_id: UUID, start_ms: int | None, end_ms: int | None, metadata: Mapping[str, Any]
) -> Turn | None:
    """One artifact row as a turn, or ``None`` if it can't be one."""
    if start_ms is None or end_ms is None or end_ms <= start_ms:
        return None
    speaker = metadata.get("speaker")
    block = metadata.get("block_start_ms")
    return Turn(
        id=artifact_id,
        start_ms=start_ms,
        end_ms=end_ms,
        speaker=speaker if isinstance(speaker, str) else None,
        block_start_ms=block if isinstance(block, int) and not isinstance(block, bool) else None,
    )


def apply_exclusions(
    turns: Iterable[Turn], exclusions: Iterable[Exclusion]
) -> tuple[list[Turn], list[Exclusion]]:
    """Drop excluded turns before grouping, so a removal can open a gap."""
    by_id = {exclusion.utterance_id: exclusion for exclusion in exclusions}
    kept, excluded = [], []
    for turn in turns:
        if turn.id in by_id:
            excluded.append(by_id[turn.id])
        else:
            kept.append(turn)
    return kept, excluded


def summarize(members: Sequence[Turn]) -> Stats:
    """Bounds, turn count, speaker set and in-block alternations of a member set.

    Labels are block-local, so a label change across blocks says nothing
    about who is speaking; only same-block changes count as alternations.
    """
    ordered = sorted(members, key=lambda t: (t.start_ms, t.end_ms))
    alternations = 0
    for prev, cur in zip(ordered, ordered[1:]):
        if (
            prev.speaker is not None
            and cur.speaker is not None
            and prev.block_start_ms == cur.block_start_ms
            and prev.speaker != cur.speaker
        ):
            alternations += 1
    speakers = tuple(sorted({t.speaker for t in ordered if t.speaker is not None}))
    return Stats(
        start_ms=ordered[0].start_ms,
        end_ms=max(t.end_ms for t in ordered),
        turns=len(ordered),
        alternations=alternations,
        speakers=speakers,
    )


def group_conversations(
    turns: Sequence[Turn], *, gap_ms: int, min_turns: int
) -> list[Conversation]:
    """Runs of turns never separated by more than ``gap_ms`` of silence.

    A run also closes at a block seam: labels are block-local, so nothing
    downstream could compare speakers across one. Silence past ``gap_ms`` is
    stamped ``gap``; a seam inside the tolerance (the block cap) ``block``.
    """
    ordered = sorted(turns, key=lambda t: (t.start_ms, t.end_ms, str(t.id)))
    runs: list[list[Turn]] = []
    closures: list[Boundary] = []  # why runs[i] closed
    end = 0
    for turn in ordered:
        if runs and turn.start_ms - end > gap_ms:
            closures.append("gap")
            runs.append([turn])
        elif runs and turn.block_start_ms != runs[-1][-1].block_start_ms:
            closures.append("block")
            runs.append([turn])
        elif runs:
            runs[-1].append(turn)
        else:
            runs.append([turn])
        end = max(end, turn.end_ms)
    closures.append("session_end")
    return _assemble(runs, closures)


def _assemble(runs: Sequence[Sequence[Turn]], closures: Sequence[Boundary]) -> list[Conversation]:
    """Conversations from adjacent runs; ``closures[i]`` is why run i ended."""
    out: list[Conversation] = []
    for index, run in enumerate(runs):
        stats = summarize(run)
        prev_end = max(t.end_ms for t in runs[index - 1]) if index > 0 else None
        next_start = runs[index + 1][0].start_ms if index + 1 < len(runs) else None
        out.append(
            Conversation(
                members=tuple(run),
                stats=stats,
                opening=closures[index - 1] if index > 0 else "session_start",
                closure=closures[index],
                gap_before_ms=None if prev_end is None else stats.start_ms - prev_end,
                gap_after_ms=None if next_start is None else next_start - stats.end_ms,
            )
        )
    return out


def user_labels(turns: Iterable[Turn]) -> dict[int | None, str]:
    """Per block, the label with the most talk — the user, who is in every
    conversation and so must not count as a participant."""
    talk: dict[int | None, dict[str, int]] = {}
    for turn in turns:
        if turn.speaker is not None:
            per_block = talk.setdefault(turn.block_start_ms, {})
            per_block[turn.speaker] = per_block.get(turn.speaker, 0) + turn.end_ms - turn.start_ms
    return {
        block: max(sorted(per_block), key=lambda label: per_block[label])
        for block, per_block in talk.items()
    }


def split_on_churn(
    conversation: Conversation,
    *,
    user: frozenset[str],
    window: int,
    min_turns: int,
) -> list[Conversation]:
    """Split where the participants change.

    At each cut, the non-user speaker sets of the ``window`` turns before
    and after must be disjoint, each side holding at least ``min_turns``
    non-user turns. Scanning resumes after a cut so one shift yields one
    split. Labels are block-local; callers guarantee one block per input.
    """
    members = list(conversation.members)
    if window < 1 or len(members) < 2 * window:
        return [conversation]

    def others(turns: Sequence[Turn]) -> list[str]:
        return [t.speaker for t in turns if t.speaker is not None and t.speaker not in user]

    cuts: list[int] = []
    i = window
    while i <= len(members) - window:
        before, after = others(members[i - window : i]), others(members[i : i + window])
        if (
            len(before) >= min_turns
            and len(after) >= min_turns
            and not set(before) & set(after)
        ):
            cuts.append(i)
            i += window
        else:
            i += 1
    if not cuts:
        return [conversation]

    bounds = [0, *cuts, len(members)]
    runs = [members[a:b] for a, b in zip(bounds, bounds[1:])]
    pieces = _assemble(runs, ["speaker_change"] * len(cuts) + [conversation.closure])
    first, last = pieces[0], pieces[-1]
    pieces[0] = Conversation(
        members=first.members,
        stats=first.stats,
        opening=conversation.opening,
        closure=first.closure,
        gap_before_ms=conversation.gap_before_ms,
        gap_after_ms=first.gap_after_ms,
    )
    pieces[-1] = Conversation(
        members=last.members,
        stats=last.stats,
        opening=last.opening,
        closure=conversation.closure,
        gap_before_ms=last.gap_before_ms,
        gap_after_ms=conversation.gap_after_ms,
    )
    return pieces


def build_conversations(
    turns: Sequence[Turn],
    *,
    gap_ms: int,
    min_turns: int,
    churn_window: int,
    churn_min_turns: int,
) -> list[tuple[Conversation, frozenset[str]]]:
    """The whole pure chain: gap/block grouping, speaker-shift splits, then
    the ``min_turns`` floor (applied last so a split's short remainder is
    dropped, not silently kept). Each result carries the user labels it was
    built with."""
    users = user_labels(turns)
    out: list[tuple[Conversation, frozenset[str]]] = []
    for conversation in group_conversations(turns, gap_ms=gap_ms, min_turns=min_turns):
        block = conversation.members[0].block_start_ms
        label = users.get(block)
        user = frozenset() if label is None else frozenset({label})
        out += [
            (piece, user)
            for piece in split_on_churn(
                conversation, user=user, window=churn_window, min_turns=churn_min_turns
            )
        ]
    return [item for item in out if item[0].stats.turns >= min_turns]


def stats_metadata(stats: Stats) -> dict[str, Any]:
    return {
        "turns": stats.turns,
        "alternations": stats.alternations,
        "speakers": list(stats.speakers),
        "speaker_count": len(stats.speakers),
    }


class ConversationPipeline(Pipeline):
    name = "conversation"
    resource = Resource.AUDIO

    async def setup(self) -> None:
        self._settings = get_settings()

    async def discover(self, limit: int) -> Sequence[JobCreate]:
        async with DatabasePipe() as pipe:
            maps = await ConversationQueries(pipe.connection).maps_without_jobs(
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
        assert job.session_id is not None  # every job here comes from discover()
        if job.artifact_id is None:
            return {"skipped": "diarize-map republished"}

        settings = self._settings
        async with DatabasePipe() as pipe:
            diarize_map = await pipe.artifacts.get(job.artifact_id)
            rows = await ConversationQueries(pipe.connection).utterances(
                job.session_id, _SOURCE_PIPELINE, _MAX_UTTERANCES
            )
        if diarize_map is None:
            return {"skipped": "diarize-map vanished"}
        if len(rows) == _MAX_UTTERANCES:
            logger.warning(
                "conversation hit the %d utterance cap for session %s",
                _MAX_UTTERANCES,
                job.session_id,
            )

        turns = [
            turn
            for row in rows
            if (turn := read_turn(row.id, row.start_ms, row.end_ms, row.metadata)) is not None
        ]
        # Build-time exclusion sources plug in here; none exist yet.
        kept, excluded = apply_exclusions(turns, ())
        conversations = build_conversations(
            kept,
            gap_ms=settings.conversation_gap_ms,
            min_turns=settings.conversation_min_turns,
            churn_window=settings.conversation_churn_window,
            churn_min_turns=settings.conversation_churn_min_turns,
        )
        return await self._publish(
            job, conversations=conversations, utterances=len(turns), excluded=excluded
        )

    async def _publish(
        self,
        job: Job,
        *,
        conversations: list[tuple[Conversation, frozenset[str]]],
        utterances: int,
        excluded: list[Exclusion],
    ) -> dict[str, Any]:
        """Replace the session's conversations atomically, map last-in-set."""
        settings = self._settings
        excluded_ids = {e.utterance_id for e in excluded}
        rows = [
            ArtifactCreate(
                pipeline=self.name,
                kind="conversation",
                session_id=job.session_id,
                start_ms=conversation.stats.start_ms,
                end_ms=conversation.stats.end_ms,
                links={"diarize_map": str(job.artifact_id)},
                metadata={
                    "utterances": [str(t.id) for t in conversation.members],
                    "excluded": [],
                    **stats_metadata(conversation.stats),
                    "opening": conversation.opening,
                    "closure": conversation.closure,
                    "gap_before_ms": conversation.gap_before_ms,
                    "gap_after_ms": conversation.gap_after_ms,
                    "user_labels": sorted(user),
                },
            )
            for conversation, user in conversations
        ]
        map_metadata: dict[str, Any] = {
            "conversations": len(conversations),
            "utterances": utterances,
            "grouped": sum(c.stats.turns for c, _ in conversations),
            "excluded": len(excluded_ids),
            "source_map": str(job.artifact_id),
            "params": {
                "gap_ms": settings.conversation_gap_ms,
                "min_turns": settings.conversation_min_turns,
                "churn_window": settings.conversation_churn_window,
                "churn_min_turns": settings.conversation_churn_min_turns,
            },
        }
        async with DatabasePipe() as pipe:
            if job.artifact_id is not None and await pipe.artifacts.get(job.artifact_id) is None:
                return {"skipped": "diarize-map republished mid-run"}
            await pipe.artifacts.delete_for_pipeline(job.session_id, self.name)
            await pipe.artifacts.create_many(rows)
            await pipe.artifacts.create(
                ArtifactCreate(
                    pipeline=self.name,
                    kind="conversation-map",
                    session_id=job.session_id,
                    metadata=map_metadata,
                )
            )
        return {
            "conversations": len(conversations),
            "utterances": utterances,
            "grouped": map_metadata["grouped"],
        }
