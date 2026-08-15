"""Expand a session, its pipeline artifacts and its raw resource segments
into the ordered event timeline.

Pure planning over rows, no I/O. Two symmetric registries feed it: each
``(pipeline, kind)`` with a registered artifact expander contributes events,
and each *resource* with a registered segment expander contributes events
straight from its stored segments (live while the session is open — no
pipeline pass needed). Everything unregistered (``speech-map``,
``session-stats``, audio segments, ...) is skipped. Registering one
expander — artifact- or segment-keyed — is all a new tier or resource needs
to appear on the timeline.
"""

from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from itertools import chain

from api.schema.timeline import (
    AudioTagEvent,
    LocationPointEvent,
    SessionEndEvent,
    SessionStartEvent,
    SoundSpanEvent,
    SpeechEndEvent,
    SpeechStartEvent,
    TimelineEvent,
    TranscriptEvent,
)
from database.schema.artifacts import PipelineArtifact
from database.schema.segments import ResourceSegment
from database.schema.sessions import RecordingSession
from database.schema.speakers import SessionSpeakerLabel

Expander = Callable[[PipelineArtifact], Iterator[TimelineEvent]]
SegmentExpander = Callable[[ResourceSegment, RecordingSession], Iterator[TimelineEvent]]

_EXPANDERS: dict[tuple[str, str], Expander] = {}
_SEGMENT_EXPANDERS: dict[str, SegmentExpander] = {}

# At one timestamp: the session opens first; span A ends before adjacent
# span B starts; a transcript follows its span's start; the session closes
# last even on an exact tie. Types without a rank land between.
_TYPE_RANK = {
    "session-start": 0,
    "speech-end": 1,
    "speech-start": 2,
    "transcript": 3,
    "audio-tag": 4,
    "sound-span": 5,
    "location-point": 6,
    "session-end": 100,
}
_DEFAULT_RANK = 50


def expander(pipeline: str, kind: str) -> Callable[[Expander], Expander]:
    """Register the expander for one ``(pipeline, kind)`` — the plug-in point."""

    def register(fn: Expander) -> Expander:
        _EXPANDERS[(pipeline, kind)] = fn
        return fn

    return register


@expander("speech-detect", "speech-span")
def _speech_span_events(artifact: PipelineArtifact) -> Iterator[TimelineEvent]:
    if artifact.start_ms is None or artifact.end_ms is None:
        return
    confidence = artifact.metadata.get("confidence")
    yield SpeechStartEvent(
        at_ms=artifact.start_ms, artifact_id=artifact.id, confidence=confidence
    )
    yield SpeechEndEvent(at_ms=artifact.end_ms, artifact_id=artifact.id, confidence=confidence)


@expander("transcribe", "transcript")
def _transcript_events(artifact: PipelineArtifact) -> Iterator[TimelineEvent]:
    if artifact.start_ms is None or artifact.end_ms is None:
        return
    text = artifact.metadata.get("text", "")
    yield TranscriptEvent(
        at_ms=artifact.start_ms,
        artifact_id=artifact.id,
        start_ms=artifact.start_ms,
        end_ms=artifact.end_ms,
        text=text,
        chars=artifact.metadata.get("chars", len(text)),
        model=artifact.metadata.get("model"),
        speaker=artifact.metadata.get("speaker"),
    )


@expander("audio-tag", "audio-tag")
def _audio_tag_events(artifact: PipelineArtifact) -> Iterator[TimelineEvent]:
    if artifact.start_ms is None or artifact.end_ms is None:
        return
    yield AudioTagEvent(
        at_ms=artifact.start_ms,
        artifact_id=artifact.id,
        start_ms=artifact.start_ms,
        end_ms=artifact.end_ms,
        labels=tuple(artifact.metadata.get("labels", ())),
        model=artifact.metadata.get("model"),
    )


@expander("sound-span", "sound-span")
def _sound_span_events(artifact: PipelineArtifact) -> Iterator[TimelineEvent]:
    label = artifact.metadata.get("label")
    if artifact.start_ms is None or artifact.end_ms is None or not label:
        return
    yield SoundSpanEvent(
        at_ms=artifact.start_ms,
        artifact_id=artifact.id,
        start_ms=artifact.start_ms,
        end_ms=artifact.end_ms,
        label=str(label),
        peak=artifact.metadata.get("peak"),
        mean=artifact.metadata.get("mean"),
        windows=artifact.metadata.get("windows"),
        model=artifact.metadata.get("model"),
    )


def segment_expander(resource: str) -> Callable[[SegmentExpander], SegmentExpander]:
    """Register the expander for one resource's raw segments."""

    def register(fn: SegmentExpander) -> SegmentExpander:
        _SEGMENT_EXPANDERS[resource] = fn
        return fn

    return register


@segment_expander("location")
def _location_events(
    segment: ResourceSegment, session: RecordingSession
) -> Iterator[TimelineEvent]:
    base_epoch_ms = int(session.started_at.timestamp() * 1000)
    for point in (segment.payload or {}).get("points", ()):
        yield LocationPointEvent(
            at_ms=point["t"] - base_epoch_ms,
            segment_id=segment.id,
            lat=point["lat"],
            lon=point["lon"],
            alt_m=point.get("alt_m"),
            accuracy_m=point.get("accuracy_m"),
            captured_at=datetime.fromtimestamp(point["t"] / 1000, tz=UTC),
        )


def _nothing(artifact: PipelineArtifact) -> Iterator[TimelineEvent]:
    return iter(())


def _session_events(session: RecordingSession) -> Iterator[TimelineEvent]:
    yield SessionStartEvent(at_ms=0, started_at=session.started_at)
    if session.ended_at is not None:
        elapsed = (session.ended_at - session.started_at) // timedelta(milliseconds=1)
        yield SessionEndEvent(at_ms=max(0, elapsed), ended_at=session.ended_at)


def timeline_resources() -> tuple[str, ...]:
    """The resources whose raw segments expand to events — what the timeline
    route must fetch."""
    return tuple(_SEGMENT_EXPANDERS)


def _sort_key(event: TimelineEvent) -> tuple[int, int, str, str]:
    source_id = getattr(event, "artifact_id", None) or getattr(event, "segment_id", None)
    return (
        event.at_ms,
        _TYPE_RANK.get(event.type, _DEFAULT_RANK),
        "" if source_id is None else str(source_id),
        event.type,
    )


def assemble(
    session: RecordingSession,
    artifacts: Sequence[PipelineArtifact],
    *,
    segments: Sequence[ResourceSegment] = (),
    from_ms: int | None = None,
    to_ms: int | None = None,
    speakers: Mapping[str, SessionSpeakerLabel] | None = None,
) -> list[TimelineEvent]:
    """Every event derivable from the rows, in timeline order.

    ``segments`` are raw resource segments (of :func:`timeline_resources`
    types) merged into the same ordered stream as artifact events; a segment
    whose resource has no expander contributes nothing.

    ``from_ms``/``to_ms`` keep only events positioned in ``[from_ms, to_ms)``:
    an artifact overlapping the window contributes just the events inside it,
    so adjacent windows partition the stream — no duplicates, no gaps.

    ``speakers`` maps block-local labels to their clustering-tier resolution;
    matching transcript events get stamped with the global identity and the
    voice-print behind it. Unresolvable labels stay null — clustering is
    eventually consistent with diarization.
    """
    events: Iterator[TimelineEvent] = chain(
        _session_events(session),
        (
            event
            for artifact in artifacts
            for event in _EXPANDERS.get((artifact.pipeline, artifact.kind), _nothing)(artifact)
        ),
        (
            event
            for segment in segments
            if (expand := _SEGMENT_EXPANDERS.get(segment.resource)) is not None
            for event in expand(segment, session)
        ),
    )
    if from_ms is not None:
        events = (event for event in events if event.at_ms >= from_ms)
    if to_ms is not None:
        events = (event for event in events if event.at_ms < to_ms)
    ordered = sorted(events, key=_sort_key)
    if not speakers:
        return ordered
    return [
        event.model_copy(
            update={
                "speaker_id": ref.speaker_id,
                "speaker_name": ref.name,
                "voiceprint_id": ref.artifact_id,
            }
        )
        if event.type == "transcript"
        and (ref := speakers.get(getattr(event, "speaker", None))) is not None
        else event
        for event in ordered
    ]
