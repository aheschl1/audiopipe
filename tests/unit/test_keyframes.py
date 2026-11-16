"""Keyframe selection over synthetic candidates and embeddings."""

import numpy as np

from processing.keyframes import KeyframeParams, select
from processing.media import Candidate


def _clip(scenes: list[int], *, step_ms: int = 500, sharp: list[float] | None = None):
    """One candidate per entry; equal scene ids share an embedding direction."""
    dims = max(scenes) + 1
    vectors = np.eye(dims)[scenes]
    sharpness = sharp or [100.0] * len(scenes)
    candidates = [
        Candidate(offset_ms=i * step_ms, jpeg=b"", sharpness=s, brightness=100.0)
        for i, s in enumerate(sharpness)
    ]
    return candidates, vectors


def _offsets(candidates, picks):
    return [(candidates[p.index].offset_ms, p.reason) for p in picks]


def test_one_scene_is_one_keyframe() -> None:
    candidates, vectors = _clip([0] * 20)
    assert _offsets(candidates, select(candidates, vectors)) == [(0, "first")]


def test_each_new_scene_opens_a_keyframe() -> None:
    candidates, vectors = _clip([0] * 6 + [1] * 6 + [0] * 6)
    # Back to scene 0 is not novel: it is still in memory.
    assert _offsets(candidates, select(candidates, vectors)) == [(0, "first"), (3000, "novel")]


def test_the_sharpest_frame_in_the_settle_window_wins() -> None:
    candidates, vectors = _clip([0, 0, 0, 0, 1, 1, 1, 1], sharp=[50, 90, 60, 70, 40, 45, 95, 80])
    picks = select(candidates, vectors, KeyframeParams(blur_percentile=0))
    assert _offsets(candidates, picks) == [(500, "first"), (3000, "novel")]


def test_min_gap_and_budget_hold_back_a_flickering_clip() -> None:
    candidates, vectors = _clip(list(range(240)), step_ms=250)  # every frame novel, 60s
    picks = select(candidates, vectors, KeyframeParams(settle_ms=0))
    gaps = np.diff([candidates[p.index].offset_ms for p in picks])
    assert gaps.min() >= 1_500
    # Novelty stops at the budget; only the heartbeat fires after that.
    assert sum(p.reason != "heartbeat" for p in picks) == 12
    assert [p.reason for p in picks[12:]] == ["heartbeat"]


def test_heartbeat_keeps_a_static_scene_represented() -> None:
    candidates, vectors = _clip([0] * 150)  # 75s of one view
    picks = select(candidates, vectors, KeyframeParams(heartbeat_ms=30_000))
    assert [p.reason for p in picks] == ["first", "heartbeat", "heartbeat"]


def test_blurred_frames_never_trigger() -> None:
    candidates, vectors = _clip([0, 0, 0, 0, 1, 0, 0, 0], sharp=[90] * 4 + [1] + [90] * 3)
    assert _offsets(candidates, select(candidates, vectors)) == [(0, "first")]


def test_all_dark_clip_still_gets_a_keyframe() -> None:
    candidates = [Candidate(i * 500, b"", 1.0, 2.0) for i in range(4)]
    assert len(select(candidates, np.eye(4)[[0] * 4])) == 1
