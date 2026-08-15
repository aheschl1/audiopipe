"""Response models for the session timeline: what happened, as events.

Every event is one moment on the session timeline (``at_ms``) carrying a
``type`` discriminator; clients must ignore types they do not recognise,
which is how future tiers (diarization, sentiment, summaries, ...) add
events without breaking existing clients.
"""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class TimelineEventBase(BaseModel):
    """Fields every event carries; concrete types add a ``Literal`` ``type``."""

    model_config = ConfigDict(frozen=True)

    at_ms: int = Field(description="Position of this event on the session timeline, ms.")


class ArtifactEventBase(TimelineEventBase):
    """An event derived from one pipeline artifact."""

    artifact_id: UUID = Field(description="The pipeline artifact this event derives from.")
    voiceprint_id: UUID | None = Field(
        None,
        description="The voice-print (embedding artifact) behind this event's "
        "speaker, when one resolved — the member id for the speaker "
        "reassign/unpin routes; null when the event carries no clustered voice.",
    )


class SegmentEventBase(TimelineEventBase):
    """An event derived directly from one resource segment — no pipeline
    involved, so it is live while the session is still open."""

    segment_id: UUID = Field(description="The resource segment this event derives from.")


class SessionStartEvent(TimelineEventBase):
    """Opens every timeline at ``at_ms`` 0."""

    type: Literal["session-start"] = "session-start"
    started_at: datetime = Field(
        description="Device-reported wall-clock start; the absolute time behind at_ms 0."
    )


class SessionEndEvent(TimelineEventBase):
    """Closes the timeline of an ended session; an open session has none.

    ``at_ms`` here is the wall-clock duration, which can drift from the
    audio-position time other events use when capture had gaps.
    """

    type: Literal["session-end"] = "session-end"
    ended_at: datetime = Field(description="Wall-clock end of the session.")


class SpeechStartEvent(ArtifactEventBase):
    """Coarse audio activity begins here (high-recall VAD, not verified speech)."""

    type: Literal["speech-start"] = "speech-start"
    confidence: float | None = Field(
        None, description="Mean VAD frame probability over the span."
    )


class SpeechEndEvent(ArtifactEventBase):
    """The activity span that started last ends here."""

    type: Literal["speech-end"] = "speech-end"
    confidence: float | None = Field(
        None, description="Mean VAD frame probability over the span."
    )


class TranscriptEvent(ArtifactEventBase):
    """What one speaker said over ``[start_ms, end_ms)``; positioned at the start."""

    type: Literal["transcript"] = "transcript"
    start_ms: int = Field(description="Start of the transcribed utterance, ms.")
    end_ms: int = Field(description="End (exclusive) of the transcribed utterance, ms.")
    text: str = Field(description="Full transcript text of the utterance.")
    chars: int = Field(description="Transcript length in characters.")
    model: str | None = Field(None, description="The transcription model that produced it.")
    speaker: str | None = Field(
        None,
        description="Diarized speaker of the utterance (block-namespaced, "
        "e.g. ``b109176:SPEAKER_00``); labels are stable within a block only. "
        "A ``.N`` suffix (``b109176:SPEAKER_00.1``) marks a sub-label minted "
        "by the purity audit when one diarizer label held several voices.",
    )
    speaker_id: UUID | None = Field(
        None,
        description="Global speaker cluster the voice resolved to; null when "
        "not (yet) clustered.",
    )
    speaker_name: str | None = Field(
        None, description="The cluster's user-given label, if it has one."
    )


class AudioTagLabel(BaseModel):
    """One scored sound-event class on a window."""

    model_config = ConfigDict(frozen=True)

    label: str = Field(description="AudioSet class name, e.g. ``Music`` or ``Typing``.")
    score: float = Field(description="The tagger's sigmoid score for the class.")


class AudioTagEvent(ArtifactEventBase):
    """Sound-event classes heard over ``[start_ms, end_ms)``; positioned at
    the start. One event per classified window (~10s); a window whose every
    class scored below the floor carries an empty list."""

    type: Literal["audio-tag"] = "audio-tag"
    start_ms: int = Field(description="Start of the classified window, ms.")
    end_ms: int = Field(description="End (exclusive) of the classified window, ms.")
    labels: tuple[AudioTagLabel, ...] = Field(
        description="Scored classes, best first, ontology ancestors suppressed."
    )
    model: str | None = Field(None, description="The tagging model that produced them.")


class SoundSpanEvent(ArtifactEventBase):
    """One continuous stretch of a single sound class over ``[start_ms,
    end_ms)``; positioned at the start.

    Spans of different classes overlap freely; spans of one class never do.
    Edges are the union of the classified windows behind them, so they are
    smeared by up to one window either way and are not event onsets.
    """

    type: Literal["sound-span"] = "sound-span"
    start_ms: int = Field(description="Start of the span, ms.")
    end_ms: int = Field(description="End (exclusive) of the span, ms.")
    label: str = Field(description="The class that held, e.g. ``Music``.")
    peak: float | None = Field(None, description="Best window score inside the span.")
    mean: float | None = Field(
        None,
        description="Mean window score across the span — how solidly the class "
        "held, where peak is how loudly it announced itself.",
    )
    windows: int | None = Field(
        None, description="How many classified windows the span merges."
    )
    model: str | None = Field(None, description="The tagging model behind the scores.")


class LocationPointEvent(SegmentEventBase):
    """One GPS fix.

    ``at_ms`` is wall-clock-derived (the fix's time minus the session start),
    which can drift from the audio-position time other events use when
    capture had gaps — the same caveat as ``session-end``.
    """

    type: Literal["location-point"] = "location-point"
    lat: float
    lon: float
    alt_m: float | None = None
    accuracy_m: float | None = None
    captured_at: datetime = Field(description="Wall-clock time of the fix.")


type TimelineEvent = Annotated[
    SessionStartEvent
    | SessionEndEvent
    | SpeechEndEvent
    | SpeechStartEvent
    | TranscriptEvent
    | AudioTagEvent
    | SoundSpanEvent
    | LocationPointEvent,
    Field(discriminator="type"),
]
