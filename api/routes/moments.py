"""Moments: wall-clock stretches of the day, described by the moment tier.

A moment spans every session that recorded in it, and carries the links the
model drew between its artifacts (a frame depicts a conversation, an
utterance reads text in a photo).
"""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status

from api.deps import CurrentUser, Pipe
from api.schema.moments import (
    ArtifactLinksResponse,
    MomentDetail,
    MomentLink,
    MomentListResponse,
    MomentRead,
)

router = APIRouter(prefix="/moments", tags=["moments"])

_NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="moment not found")


@router.get("", summary="Your moments, newest first")
async def list_moments(
    user: CurrentUser,
    pipe: Pipe,
    before: datetime | None = Query(None, description="Only moments starting before this instant."),
    limit: int = Query(20, ge=1, le=100),
) -> MomentListResponse:
    moments = await pipe.moments.list_for_user(user.id, before=before, limit=limit)
    return MomentListResponse(
        items=[MomentRead.from_model(m) for m in moments],
        next_before=moments[-1].start_at if len(moments) == limit else None,
    )


@router.get("/links/{artifact_id}", summary="Links touching one artifact")
async def artifact_links(user: CurrentUser, pipe: Pipe, artifact_id: UUID) -> ArtifactLinksResponse:
    """Every relation any of your moments drew to or from this artifact."""
    links = await pipe.moments.links_for_artifact(artifact_id, user.id)
    return ArtifactLinksResponse(
        artifact_id=artifact_id,
        links=[MomentLink.from_model(link) for link in links],
        moment_ids=sorted({link.moment_id for link in links}, key=str),
    )


@router.get("/at", summary="The moment containing a point in a recording")
async def moment_at(
    user: CurrentUser,
    pipe: Pipe,
    session_id: UUID = Query(description="The recording the point is in."),
    at_ms: int = Query(description="Session-relative ms, as search hits and timeline events give it."),
) -> MomentRead:
    """For opening a search hit as a moment. Falls back to the nearest of the
    session's moments within an hour; 404 when it has none."""
    moment = await pipe.moments.at(user.id, session_id, at_ms)
    if moment is None:
        raise _NOT_FOUND
    return MomentRead.from_model(moment)


@router.get("/{moment_id}", summary="One moment with its items and links")
async def get_moment(user: CurrentUser, pipe: Pipe, moment_id: UUID) -> MomentDetail:
    moment = await pipe.moments.get(moment_id, user.id)
    if moment is None:
        raise _NOT_FOUND
    previous_id, next_id = await pipe.moments.neighbours(moment)
    return MomentDetail.build(moment, await pipe.moments.links(moment.id), previous_id, next_id)
