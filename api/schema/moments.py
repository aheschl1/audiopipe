"""Response models for moments: described stretches of the day and their link graph."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from database.repos.moments import ArtifactLink, Moment

Relation = Literal["depicts", "mentions", "reads", "same_scene", "unrelated"]


class MomentCounts(BaseModel):
    model_config = ConfigDict(frozen=True)

    utterances: int
    frames: int
    photos: int
    links: int


class MomentCover(BaseModel):
    model_config = ConfigDict(frozen=True)

    artifact_id: UUID = Field(description="A keyframe; its JPEG is at /v1/artifacts/{id}/media.")
    session_id: UUID
    width: int
    height: int


class MomentItem(BaseModel):
    """One labelled artifact the model was shown."""

    model_config = ConfigDict(frozen=True)

    artifact_id: UUID
    label: str = Field(description="The label the model saw, e.g. U3, K2, P1, S1.")
    kind: Literal["utterance", "keyframe", "photo", "sound"]
    session_id: UUID
    at_ms: int | None = Field(description="Session-relative ms, for opening the session view there.")
    start_at: datetime
    end_at: datetime | None
    text: str | None = Field(description="The utterance's words, or the sound span's class.")
    speaker: str | None = Field(description="Speaker name if curated, else 'voice N'.")
    speaker_id: UUID | None = Field(None, description="The speaker cluster, when the voice is clustered.")
    segment_id: UUID | None
    offset_ms: int | None
    width: int | None
    height: int | None


class MomentSpeaker(BaseModel):
    """Someone heard in the moment, from its transcripts (not the model's guess)."""

    model_config = ConfigDict(frozen=True)

    name: str
    speaker_id: UUID | None


class MomentLink(BaseModel):
    model_config = ConfigDict(frozen=True)

    from_artifact_id: UUID
    to_artifact_id: UUID
    relation: Relation
    confidence: float
    why: str

    @classmethod
    def from_model(cls, link: ArtifactLink) -> "MomentLink":
        return cls(
            from_artifact_id=link.from_artifact,
            to_artifact_id=link.to_artifact,
            relation=link.relation,  # type: ignore[arg-type]
            confidence=link.confidence,
            why=link.why,
        )


def _speakers(items) -> list[MomentSpeaker]:
    """Distinct speakers in order of first speech."""
    seen: dict[str, MomentSpeaker] = {}
    for item in sorted(items, key=lambda i: i.start_at):
        if item.kind == "utterance" and item.speaker and item.speaker not in seen:
            seen[item.speaker] = MomentSpeaker(name=item.speaker, speaker_id=item.speaker_id)
    return list(seen.values())


class MomentRead(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    start_at: datetime
    end_at: datetime
    title: str
    summary: str
    people: list[str] = Field(description="Participants as the model named them; prefer ``speakers``.")
    speakers: list[MomentSpeaker]
    places: list[str]
    visible_text: list[str]
    session_ids: list[UUID]
    counts: MomentCounts
    cover: list[MomentCover]
    model: str
    created_at: datetime

    @staticmethod
    def _items(moment: Moment) -> list[MomentItem]:
        return [MomentItem.model_validate(i) for i in moment.metadata.get("items", [])]

    @classmethod
    def from_model(cls, moment: Moment, **extra: Any) -> "MomentRead":
        meta = moment.metadata
        items = {str(i.artifact_id): i for i in cls._items(moment)}
        covers = [
            MomentCover(
                artifact_id=item.artifact_id, session_id=item.session_id,
                width=item.width or 0, height=item.height or 0,
            )
            for artifact_id in meta.get("cover", [])
            if (item := items.get(artifact_id)) is not None
        ]
        return cls(
            id=moment.id,
            start_at=moment.start_at,
            end_at=moment.end_at,
            title=moment.title,
            summary=moment.summary,
            people=list(meta.get("people", [])),
            speakers=_speakers(items.values()),
            places=list(meta.get("places", [])),
            visible_text=list(meta.get("visible_text", [])),
            session_ids=moment.session_ids,
            counts=MomentCounts.model_validate(
                meta.get("counts") or {"utterances": 0, "frames": 0, "photos": 0, "links": 0}
            ),
            cover=covers,
            model=moment.model,
            created_at=moment.created_at,
            **extra,
        )


class MomentDetail(MomentRead):
    items: list[MomentItem]
    links: list[MomentLink]
    previous_id: UUID | None = Field(description="Your moment just before this one.")
    next_id: UUID | None = Field(description="Your moment just after this one.")

    @classmethod
    def build(
        cls,
        moment: Moment,
        links: list[ArtifactLink],
        previous_id: UUID | None,
        next_id: UUID | None,
    ) -> "MomentDetail":
        return cls.from_model(
            moment,
            items=cls._items(moment),
            links=[MomentLink.from_model(link) for link in links],
            previous_id=previous_id,
            next_id=next_id,
        )


class MomentListResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: list[MomentRead]
    next_before: datetime | None = Field(description="Pass as ``before`` for the next (older) page; null at the end.")


class ArtifactLinksResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    artifact_id: UUID
    links: list[MomentLink]
    moment_ids: list[UUID] = Field(description="The moments that asserted these links.")
