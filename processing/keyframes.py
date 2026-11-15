"""Keyframe selection: the few frames of a clip worth captioning, OCR-ing and searching.

Candidates arrive at a fixed rate with focus/exposure scores and an image
embedding each. A frame opens a pick when it passes the quality gate and is
far enough (cosine) from every recent keyframe, or when the clip has gone a
heartbeat without one. The pick itself is the sharpest gated frame within a
short settle window after the trigger, so a camera still mid-swing doesn't win.
"""

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from processing.media import Candidate


@dataclass(frozen=True)
class KeyframeParams:
    novelty: float = 0.10
    memory: int = 8
    settle_ms: int = 1_000
    min_gap_ms: int = 1_500
    heartbeat_ms: int = 30_000
    max_per_minute: int = 12
    blur_percentile: float = 25.0
    min_brightness: float = 15.0
    max_brightness: float = 240.0


@dataclass(frozen=True)
class Pick:
    index: int
    reason: str  # "first" | "novel" | "heartbeat"
    novelty: float


def _gate(candidates: Sequence[Candidate], p: KeyframeParams) -> list[bool]:
    floor = float(np.percentile([c.sharpness for c in candidates], p.blur_percentile))
    ok = [
        p.min_brightness <= c.brightness <= p.max_brightness and c.sharpness >= floor
        for c in candidates
    ]
    # A clip that is all dark or all blur still gets keyframes, just unfiltered.
    return ok if any(ok) else [True] * len(candidates)


def select(
    candidates: Sequence[Candidate], vectors: np.ndarray, p: KeyframeParams = KeyframeParams()
) -> list[Pick]:
    """Keyframe indices into ``candidates`` (time-ordered; ``vectors`` L2-normalized rows)."""
    n = len(candidates)
    if n == 0:
        return []
    ok = _gate(candidates, p)
    picks: list[Pick] = []
    memory: list[int] = []

    def novelty(i: int) -> float:
        return float(1 - (vectors[memory] @ vectors[i]).max()) if memory else 1.0

    def settle(i: int) -> int:
        horizon = candidates[i].offset_ms + p.settle_ms
        window = [j for j in range(i, n) if ok[j] and candidates[j].offset_ms <= horizon]
        return max(window, key=lambda j: candidates[j].sharpness)

    i = 0
    while i < n:
        if not ok[i]:
            i += 1
            continue
        at = candidates[i].offset_ms
        if not picks:
            reason = "first"
        else:
            since = at - candidates[picks[-1].index].offset_ms
            recent = sum(1 for k in picks if candidates[k.index].offset_ms > at - 60_000)
            if since < p.min_gap_ms:
                i += 1
                continue
            if novelty(i) >= p.novelty and recent < p.max_per_minute:
                reason = "novel"
            elif since >= p.heartbeat_ms:
                reason = "heartbeat"
            else:
                i += 1
                continue
        j = settle(i)
        picks.append(Pick(j, reason, novelty(j)))
        memory = [*memory, j][-p.memory :]
        i = j + 1
    return picks
