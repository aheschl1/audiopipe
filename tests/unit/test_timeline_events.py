"""Timeline assembly: expansion, ordering, and window clipping. No stores."""

import random
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from api.timeline_events import assemble
from database.schema.artifacts import PipelineArtifact
from database.schema.sessions import RecordingSession
from database.schema.speakers import SessionSpeakerLabel

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _session(ended_after_ms: int | None = None) -> RecordingSession:
    return RecordingSession(
        id=uuid4(),
        user_id=uuid4(),
        started_at=_NOW,
        ended_at=None
        if ended_after_ms is None
        else _NOW + timedelta(milliseconds=ended_after_ms),
        created_at=_NOW,
        updated_at=_NOW,
    )


def _artifact(
    pipeline: str,
    kind: str,
    start_ms: int | None = None,
    end_ms: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> PipelineArtifact:
    return PipelineArtifact(
        id=uuid4(),
        pipeline=pipeline,
        kind=kind,
        session_id=uuid4(),
        start_ms=start_ms,
        end_ms=end_ms,
        metadata=metadata or {},
        created_at=_NOW,
        updated_at=_NOW,
    )


def _span(start_ms: int, end_ms: int, confidence: float = 0.9) -> PipelineArtifact:
    return _artifact(
        "speech-detect", "speech-span", start_ms, end_ms, {"confidence": confidence}
    )


def _transcript(
    start_ms: int, end_ms: int, text: str = "hello", speaker: str | None = "b0:SPEAKER_00"
) -> PipelineArtifact:
    return _artifact(
        "transcribe",
        "transcript",
        start_ms,
        end_ms,
        {"text": text, "chars": len(text), "model": "stt-1", "speaker": speaker},
    )


def _sound_span(
    start_ms: int | None,
    end_ms: int | None,
    label: str = "Music",
    peak: float | None = 0.82,
    mean: float | None = 0.61,
    windows: int | None = 14,
) -> PipelineArtifact:
    return _artifact(
        "sound-span",
        "sound-span",
        start_ms,
        end_ms,
        {
            "label": label,
            "peak": peak,
            "mean": mean,
            "windows": windows,
            "model": "ced-base",
        },
    )


def _shape(events) -> list[tuple[str, int]]:
    return [(event.type, event.at_ms) for event in events]


def test_orders_session_frames_spans_and_transcripts() -> None:
    span_a, span_b = _span(0, 5_000), _span(5_000, 9_000, confidence=0.8)
    rows = [span_a, _transcript(0, 5_000), span_b, _transcript(5_000, 9_000)]

    events = assemble(_session(ended_after_ms=9_000), rows)

    # Adjacent spans: A ends before B starts at 5000; the transcript follows
    # its span's start; session-end wins the tie at 9000.
    assert _shape(events) == [
        ("session-start", 0),
        ("speech-start", 0),
        ("transcript", 0),
        ("speech-end", 5_000),
        ("speech-start", 5_000),
        ("transcript", 5_000),
        ("speech-end", 9_000),
        ("session-end", 9_000),
    ]
    assert events[0].started_at == _NOW
    assert events[-1].ended_at == _NOW + timedelta(milliseconds=9_000)
    assert events[1].artifact_id == span_a.id and events[1].confidence == 0.9
    assert events[3].artifact_id == span_a.id
    assert events[4].artifact_id == span_b.id and events[4].confidence == 0.8


def test_open_session_has_no_end_frame_and_unknown_kinds_are_skipped() -> None:
    rows = [
        _artifact("speech-detect", "speech-map", metadata={"spans": 0}),
        _artifact("session-stats", "session-stats"),
        _artifact("future", "sentiment", 1_000, 2_000),
    ]
    assert _shape(assemble(_session(), rows)) == [("session-start", 0)]


def test_window_keeps_only_events_positioned_inside_it() -> None:
    rows = [_span(3_000, 8_000), _transcript(3_000, 8_000)]

    inside = assemble(_session(), rows, from_ms=4_000, to_ms=10_000)
    assert _shape(inside) == [("speech-end", 8_000)]  # start/transcript/session-start clipped

    assert assemble(_session(), rows, from_ms=4_000, to_ms=8_000) == []  # half-open
    assert assemble(_session(), rows, from_ms=10_000, to_ms=20_000) == []


def test_adjacent_windows_partition_the_stream() -> None:
    session = _session(ended_after_ms=9_000)
    rows = [_span(0, 5_000), _transcript(0, 5_000), _span(5_000, 9_000), _transcript(5_000, 9_000)]

    first = assemble(session, rows, from_ms=0, to_ms=5_000)
    second = assemble(session, rows, from_ms=5_000, to_ms=10_000)
    assert first + second == assemble(session, rows)


def test_orphan_transcript_still_appears() -> None:
    events = assemble(_session(), [_transcript(2_000, 4_000, text="alone")])
    assert _shape(events) == [("session-start", 0), ("transcript", 2_000)]
    assert events[1].text == "alone" and events[1].end_ms == 4_000


def test_speaker_map_stamps_global_identity_on_transcripts() -> None:
    resolved = _transcript(0, 1_000, speaker="b0:SPEAKER_00")
    unresolved = _transcript(2_000, 3_000, speaker="b0:SPEAKER_01")
    speaker_id, voiceprint_id = uuid4(), uuid4()

    events = assemble(
        _session(),
        [resolved, unresolved],
        speakers={
            "b0:SPEAKER_00": SessionSpeakerLabel(
                speaker="b0:SPEAKER_00",
                artifact_id=voiceprint_id,
                speaker_id=speaker_id,
                name="Mom",
            )
        },
    )

    stamped, plain = events[1:]
    assert stamped.speaker_id == speaker_id and stamped.speaker_name == "Mom"
    assert stamped.voiceprint_id == voiceprint_id  # the reassign handle
    assert stamped.speaker == "b0:SPEAKER_00"  # provenance survives
    assert plain.speaker_id is None and plain.speaker_name is None
    assert plain.voiceprint_id is None

    # No map at all: nothing is stamped, nothing breaks.
    bare = assemble(_session(), [resolved])
    assert bare[1].speaker_id is None and bare[1].speaker_name is None


def test_speaker_map_names_can_be_null() -> None:
    # An unlabeled cluster still resolves the id; the name stays null.
    events = assemble(
        _session(),
        [_transcript(0, 1_000, speaker="b0:SPEAKER_00")],
        speakers={
            "b0:SPEAKER_00": SessionSpeakerLabel(
                speaker="b0:SPEAKER_00", artifact_id=uuid4(), speaker_id=uuid4()
            )
        },
    )
    assert events[1].speaker_id is not None and events[1].speaker_name is None


def test_transcript_speaker_and_missing_metadata() -> None:
    attributed = _artifact(
        "transcribe",
        "transcript",
        0,
        1_000,
        {"text": "hi", "chars": 2, "speaker": "b0:SPEAKER_01"},
    )
    bare = _artifact("transcribe", "transcript", 2_000, 3_000)

    spoken, empty = assemble(_session(), [attributed, bare])[1:]

    assert spoken.speaker == "b0:SPEAKER_01" and spoken.chars == 2
    assert empty.text == "" and empty.chars == 0 and empty.speaker is None
    assert empty.model is None


def test_missing_confidence_and_null_spans_are_tolerated() -> None:
    no_confidence = _artifact("speech-detect", "speech-span", 0, 1_000)
    null_span = _artifact("speech-detect", "speech-span")

    events = assemble(_session(), [no_confidence, null_span])

    assert _shape(events) == [("session-start", 0), ("speech-start", 0), ("speech-end", 1_000)]
    assert events[1].confidence is None


def test_order_is_deterministic_under_input_shuffle() -> None:
    session = _session(ended_after_ms=9_000)
    # Includes a duplicated span (reprocessing) sharing its whole range.
    rows = [_span(0, 5_000), _span(0, 5_000), _transcript(0, 5_000), _span(5_000, 9_000)]
    expected = assemble(session, rows)

    shuffled = rows[:]
    random.Random(0).shuffle(shuffled)
    assert assemble(session, shuffled) == expected
    assert assemble(session, list(reversed(rows))) == expected


def test_end_frame_never_precedes_the_start_frame() -> None:
    # A device clock stepping backwards must not produce a negative position.
    session = RecordingSession(
        id=uuid4(),
        user_id=uuid4(),
        started_at=_NOW,
        ended_at=_NOW - timedelta(seconds=5),
        created_at=_NOW,
        updated_at=_NOW,
    )
    assert _shape(assemble(session, [])) == [("session-start", 0), ("session-end", 0)]


def test_no_artifacts_serves_only_session_frames() -> None:
    assert _shape(assemble(_session(), [])) == [("session-start", 0)]


def test_sound_span_expands_to_one_event_carrying_its_interval() -> None:
    row = _sound_span(5_000, 65_000)
    events = assemble(_session(), [row])
    assert _shape(events) == [("session-start", 0), ("sound-span", 5_000)]
    span = events[1]
    assert (span.start_ms, span.end_ms) == (5_000, 65_000)
    assert (span.label, span.peak, span.mean, span.windows) == ("Music", 0.82, 0.61, 14)
    assert span.model == "ced-base"
    assert span.artifact_id == row.id


def test_overlapping_sound_spans_of_different_classes_both_survive() -> None:
    rows = [
        _sound_span(0, 60_000, "Music"),
        _sound_span(30_000, 90_000, "Speech"),
    ]
    events = assemble(_session(), rows)
    assert _shape(events) == [
        ("session-start", 0),
        ("sound-span", 0),
        ("sound-span", 30_000),
    ]
    assert [event.label for event in events[1:]] == ["Music", "Speech"]
    # The intervals genuinely overlap — that is the tier's output shape.
    assert events[2].at_ms < events[1].end_ms

    shuffled = list(reversed(rows))
    random.Random(0).shuffle(shuffled)
    assert assemble(_session(), shuffled) == events


def test_sound_span_tolerates_thin_metadata() -> None:
    session = _session()
    assert _shape(assemble(session, [_artifact("sound-span", "sound-span", 0, 10)])) == [
        ("session-start", 0)
    ]
    assert _shape(assemble(session, [_sound_span(None, None)])) == [("session-start", 0)]

    events = assemble(
        session, [_artifact("sound-span", "sound-span", 0, 10, {"label": "Rain"})]
    )
    span = events[1]
    assert span.label == "Rain"
    assert (span.peak, span.mean, span.windows, span.model) == (None, None, None, None)


def test_sound_span_window_keeps_only_spans_starting_inside() -> None:
    # Accepted behaviour, pinned deliberately: the window filters on event
    # position, so a span that began before it is dropped rather than clipped.
    rows = [_sound_span(0, 600_000)]
    assert assemble(_session(), rows, from_ms=120_000, to_ms=240_000) == []


def _location_segment(session: RecordingSession, points: list[dict]) -> Any:
    from database.schema.segments import ResourceSegment

    return ResourceSegment(
        id=uuid4(),
        session_id=session.id,
        user_id=session.user_id,
        resource="location",
        sequence=0,
        ingested_at=_NOW,
        payload={"points": points},
        byte_size=1,
        content_type="application/json",
    )


def _epoch_ms(at: datetime) -> int:
    return int(at.timestamp() * 1000)


def test_location_segments_expand_per_point() -> None:
    session = _session()
    base = _epoch_ms(_NOW)
    segment = _location_segment(
        session,
        [
            {"lat": 51.0, "lon": -114.0, "t": base + 1_000, "accuracy_m": 4.0},
            {"lat": 51.1, "lon": -114.1, "t": base + 2_500, "alt_m": 1045.0},
        ],
    )
    events = assemble(session, [], segments=[segment])
    points = [event for event in events if event.type == "location-point"]
    assert [(p.at_ms, p.lat, p.lon) for p in points] == [
        (1_000, 51.0, -114.0),
        (2_500, 51.1, -114.1),
    ]
    assert points[0].accuracy_m == 4.0 and points[0].alt_m is None
    assert points[1].alt_m == 1045.0
    assert points[0].segment_id == segment.id
    assert points[0].captured_at == _NOW + timedelta(seconds=1)


def test_location_points_interleave_and_window_partitions() -> None:
    session = _session(ended_after_ms=10_000)
    base = _epoch_ms(_NOW)
    segment = _location_segment(
        session,
        [{"lat": 51.0, "lon": -114.0, "t": base + at} for at in (500, 2_000, 6_000)],
    )
    transcript = _artifact(
        "transcribe", "transcript", start_ms=2_000, end_ms=3_000, metadata={"text": "hi"}
    )

    everything = assemble(session, [transcript], segments=[segment])
    assert [event.type for event in everything] == [
        "session-start",
        "location-point",
        "transcript",       # transcript ranks before location-point on a tie
        "location-point",
        "location-point",
        "session-end",
    ]

    first = assemble(session, [transcript], segments=[segment], from_ms=0, to_ms=2_000)
    second = assemble(
        session, [transcript], segments=[segment], from_ms=2_000, to_ms=10_000
    )
    assert [e.at_ms for e in first if e.type == "location-point"] == [500]
    assert [e.at_ms for e in second if e.type == "location-point"] == [2_000, 6_000]


def test_unregistered_resource_segments_are_skipped() -> None:
    from database.schema.segments import ResourceSegment

    session = _session()
    audio = ResourceSegment(
        id=uuid4(),
        session_id=session.id,
        user_id=session.user_id,
        resource="audio",
        sequence=0,
        ingested_at=_NOW,
        bucket="b",
        object_key="k",
        byte_size=10,
        content_type="audio/wav",
    )
    events = assemble(session, [], segments=[audio])
    assert [event.type for event in events] == ["session-start"]


def test_empty_location_payload_yields_nothing() -> None:
    from database.schema.segments import ResourceSegment

    session = _session()
    segment = ResourceSegment(
        id=uuid4(),
        session_id=session.id,
        user_id=session.user_id,
        resource="location",
        sequence=0,
        ingested_at=_NOW,
        payload={},
        byte_size=1,
        content_type="application/json",
    )
    events = assemble(session, [], segments=[segment])
    assert [event.type for event in events] == ["session-start"]
