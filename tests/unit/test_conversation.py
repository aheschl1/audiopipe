"""The conversation tier's pure logic: turn reading, exclusion, grouping, stats."""

from uuid import uuid4

from processing.pipelines.conversation import (
    Exclusion,
    Turn,
    apply_exclusions,
    build_conversations,
    group_conversations,
    read_turn,
    split_on_churn,
    summarize,
    user_labels,
)

GAP = 60_000


def _t(start: int, end: int, speaker: str = "b0:SPEAKER_00", block: int = 0) -> Turn:
    return Turn(id=uuid4(), start_ms=start, end_ms=end, speaker=speaker, block_start_ms=block)


def _group(turns, **overrides):
    """The chain with the speaker-shift pass off (window 0)."""
    params = {"gap_ms": GAP, "min_turns": 2, "churn_window": 0, "churn_min_turns": 1}
    return [c for c, _ in build_conversations(turns, **{**params, **overrides})]


class TestReadTurn:
    def test_valid_row(self) -> None:
        uid = uuid4()
        turn = read_turn(uid, 0, 1000, {"speaker": "b0:SPEAKER_01", "block_start_ms": 0})
        assert turn == Turn(uid, 0, 1000, "b0:SPEAKER_01", 0)

    def test_null_or_inverted_bounds_are_not_turns(self) -> None:
        assert read_turn(uuid4(), None, 1000, {}) is None
        assert read_turn(uuid4(), 0, None, {}) is None
        assert read_turn(uuid4(), 1000, 1000, {}) is None

    def test_malformed_metadata_is_tolerated(self) -> None:
        turn = read_turn(uuid4(), 0, 1000, {"speaker": 3, "block_start_ms": "x"})
        assert turn is not None
        assert turn.speaker is None and turn.block_start_ms is None


class TestGrouping:
    def test_turns_within_gap_form_one_conversation(self) -> None:
        turns = [_t(0, 1000), _t(2000, 3000, "b0:SPEAKER_01"), _t(3000 + GAP, 4000 + GAP)]
        (conversation,) = _group(turns)
        assert conversation.stats.start_ms == 0
        assert conversation.stats.end_ms == 4000 + GAP
        assert conversation.stats.turns == 3

    def test_gap_past_threshold_closes(self) -> None:
        turns = [_t(0, 1000), _t(2000, 3000), _t(3001 + GAP, 5000 + GAP), _t(6000 + GAP, 7000 + GAP)]
        first, second = _group(turns)
        assert (first.stats.start_ms, first.stats.end_ms) == (0, 3000)
        assert (second.stats.start_ms, second.stats.end_ms) == (3001 + GAP, 7000 + GAP)
        assert first.closure == "gap" and second.opening == "gap"
        assert first.gap_after_ms == second.gap_before_ms == 1 + GAP

    def test_lone_utterance_is_not_a_conversation(self) -> None:
        assert _group([_t(0, 1000)]) == []
        turns = [_t(0, 1000), _t(2000, 3000), _t(100_000 + GAP, 101_000 + GAP)]
        (conversation,) = _group(turns)
        assert conversation.stats.turns == 2
        # The dropped tail still counts as the neighbour that closed the run.
        assert conversation.closure == "gap"

    def test_min_turns(self) -> None:
        turns = [_t(0, 1000), _t(2000, 3000)]
        assert _group(turns, min_turns=3) == []

    def test_session_edges(self) -> None:
        (conversation,) = _group([_t(0, 1000), _t(2000, 3000)])
        assert conversation.opening == "session_start"
        assert conversation.closure == "session_end"
        assert conversation.gap_before_ms is None and conversation.gap_after_ms is None

    def test_gap_measures_from_the_furthest_end(self) -> None:
        # An interjection nested in a long host must not shorten the run's reach.
        turns = [_t(0, 50_000), _t(1000, 2000, "b0:SPEAKER_01"), _t(50_000 + GAP, 51_000 + GAP)]
        (conversation,) = _group(turns)
        assert conversation.stats.turns == 3

    def test_block_seam_closes_inside_the_gap_tolerance(self) -> None:
        # The block cap cut mid-speech: labels restart, so the run must too.
        turns = [_t(0, 1000, block=0), _t(2000, 3000, block=0), _t(4000, 5000, "b4000:SPEAKER_00", 4000), _t(6000, 7000, "b4000:SPEAKER_01", 4000)]
        first, second = _group(turns)
        assert first.closure == "block" and second.opening == "block"
        assert second.gap_before_ms == 1000

    def test_silence_past_gap_is_stamped_gap_even_at_a_seam(self) -> None:
        turns = [_t(0, 1000), _t(2000, 3000), _t(3001 + GAP, 4000 + GAP, "b1:S", 3001 + GAP), _t(5000 + GAP, 6000 + GAP, "b1:S", 3001 + GAP)]
        first, _ = _group(turns)
        assert first.closure == "gap"

    def test_input_order_is_not_trusted(self) -> None:
        turns = [_t(2000, 3000), _t(0, 1000)]
        (conversation,) = _group(turns)
        assert [t.start_ms for t in conversation.members] == [0, 2000]


class TestSummarize:
    def test_alternations_count_within_a_block_only(self) -> None:
        turns = [
            _t(0, 1000, "b0:SPEAKER_00", 0),
            _t(1000, 2000, "b0:SPEAKER_01", 0),
            _t(2000, 3000, "b0:SPEAKER_01", 0),
            _t(40_000, 41_000, "b40000:SPEAKER_00", 40_000),  # new block, new label namespace
            _t(41_000, 42_000, "b40000:SPEAKER_01", 40_000),
        ]
        stats = summarize(turns)
        assert stats.alternations == 2
        assert stats.speakers == (
            "b0:SPEAKER_00",
            "b0:SPEAKER_01",
            "b40000:SPEAKER_00",
            "b40000:SPEAKER_01",
        )

    def test_single_speaker_run_has_no_alternations(self) -> None:
        stats = summarize([_t(0, 1000), _t(2000, 3000), _t(4000, 5000)])
        assert stats.alternations == 0
        assert stats.speakers == ("b0:SPEAKER_00",)
        assert (stats.start_ms, stats.end_ms, stats.turns) == (0, 5000, 3)


class TestExclusions:
    def test_excluded_turn_is_removed_and_reported(self) -> None:
        noise = _t(2000, 3000)
        turns = [_t(0, 1000), noise, _t(4000, 5000)]
        kept, excluded = apply_exclusions(turns, [Exclusion(noise.id, "tv", "manual")])
        assert [t.start_ms for t in kept] == [0, 4000]
        assert excluded == [Exclusion(noise.id, "tv", "manual")]

    def test_exclusion_can_open_a_gap_and_split(self) -> None:
        bridge = _t(30_000, 31_000)
        turns = [_t(0, 1000), _t(2000, 3000), bridge, _t(60_000, 61_000), _t(62_000, 63_000)]
        assert len(_group(turns, gap_ms=40_000)) == 1
        kept, _ = apply_exclusions(turns, [Exclusion(bridge.id, None, "manual")])
        assert len(_group(kept, gap_ms=40_000)) == 2

    def test_unknown_exclusion_is_ignored(self) -> None:
        turns = [_t(0, 1000)]
        kept, excluded = apply_exclusions(turns, [Exclusion(uuid4(), None, "manual")])
        assert kept == turns and excluded == []


def _conv(turns, gap=GAP):
    (conversation,) = group_conversations(turns, gap_ms=gap, min_turns=1)
    return conversation


def _dialogue(*labels: str, user: str = "me") -> list:
    """Alternating user/other turns, 1 s each, from a list of others' labels."""
    out, t = [], 0
    for label in labels:
        out.append(_t(t, t + 1000, label if label != "me" else user))
        t += 1500
    return out


class TestUserLabel:
    def test_dominant_talk_per_block(self) -> None:
        turns = [_t(0, 5000, "me"), _t(5000, 6000, "A"), _t(9000, 9500, "B", block=9000), _t(9500, 12_000, "A", block=9000)]
        assert user_labels(turns) == {0: "me", 9000: "A"}


class TestSplitOnChurn:
    def test_persistent_change_splits(self) -> None:
        turns = _dialogue("me", "A", "me", "A", "me", "A", "B", "me", "B", "me", "B", "me")
        pieces = split_on_churn(_conv(turns), user=frozenset({"me"}), window=4, min_turns=2)
        assert len(pieces) == 2
        first, second = pieces
        assert first.closure == "speaker_change" and second.opening == "speaker_change"
        assert first.stats.speakers == ("A", "me") and second.stats.speakers == ("B", "me")
        assert first.gap_after_ms == second.gap_before_ms == 500

    def test_one_interjection_does_not_split(self) -> None:
        turns = _dialogue("me", "A", "me", "A", "C", "me", "A", "me", "A", "me")
        assert len(split_on_churn(_conv(turns), user=frozenset({"me"}), window=4, min_turns=2)) == 1

    def test_user_alone_never_splits(self) -> None:
        # A phone call: the user is the only voice.
        turns = _dialogue("me", "me", "me", "me", "me", "me", "me", "me")
        assert len(split_on_churn(_conv(turns), user=frozenset({"me"}), window=4, min_turns=2)) == 1

    def test_outer_boundaries_are_preserved(self) -> None:
        turns = _dialogue("me", "A", "me", "A", "B", "me", "B", "me")
        source = _conv(turns)
        first, last = split_on_churn(source, user=frozenset({"me"}), window=2, min_turns=1)
        assert first.opening == source.opening == "session_start"
        assert last.closure == source.closure == "session_end"

    def test_too_short_to_compare(self) -> None:
        turns = _dialogue("me", "A", "B")
        assert len(split_on_churn(_conv(turns), user=frozenset({"me"}), window=4, min_turns=2)) == 1


class TestBuildConversations:
    def test_chain_applies_floor_after_splits(self) -> None:
        # A→B shift whose B side is one non-user turn short of min_turns=3.
        turns = _dialogue("me", "A", "me", "A", "me", "A", "B", "me")
        out = build_conversations(turns, gap_ms=GAP, min_turns=3, churn_window=2, churn_min_turns=1)
        assert [c.stats.turns for c, _ in out] == [6]
        assert out[0][1] == frozenset({"me"})
