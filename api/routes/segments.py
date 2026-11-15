"""Resource segments: metadata, and the stored bytes themselves. Read-only."""

import json

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import StreamingResponse

from api.deps import CurrentUser, OwnedSegment, Paging, Pipe, PlayableSegment
from api.blob_media import blob_response
from api.ranges import etag_matches
from api.schema.common import Page
from api.schema.segments import SegmentRead, segment_read
from database.schema.segments import ResourceSegment
from storage.keys import suffix_for

router = APIRouter(prefix="/segments", tags=["segments"])


@router.get("", summary="List your segments")
async def list_segments(
    user: CurrentUser,
    pipe: Pipe,
    paging: Paging,
    resource: str | None = Query(None, description="Only segments of this resource."),
) -> Page[SegmentRead]:
    """Across every session the caller owns, most recently ingested first."""
    rows = await pipe.segments.list_for_user(
        user.id, resource=resource, limit=paging.probe_limit, offset=paging.offset
    )
    return Page.build(rows, paging, segment_read)


@router.get("/{segment_id}", summary="Fetch one segment's metadata")
async def get_segment(segment: OwnedSegment) -> SegmentRead:
    return segment_read(segment)


def _etag(segment: ResourceSegment) -> str:
    """Strong validator: the stored SHA-256, or id + size when it is unset."""
    return f'"{segment.checksum_hex or f"{segment.id}-{segment.byte_size}"}"'


@router.get(
    "/{segment_id}/media",
    summary="Download a segment's stored bytes",
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {"audio/*": {}, "application/json": {}},
            "description": "The stored object (blob resources) or inline payload.",
        },
        206: {"content": {"audio/*": {}}, "description": "The requested byte range."},
        304: {"description": "The client's cached copy is still current."},
        416: {"description": "The requested range lies outside the object."},
    },
)
async def get_segment_media(segment: PlayableSegment, request: Request) -> Response:
    """Serve what the segment stores: the blob for blob-backed resources
    (honouring `Range` and conditional requests), the inline payload as JSON
    otherwise.

    A `Range` is passed through to the object store, so only that slice is
    transferred. Segments are immutable, so `If-None-Match` answers 304 without
    touching the store.
    """
    etag = _etag(segment)
    filename = f"{segment.sequence:06d}-{segment.id}{suffix_for(segment.content_type)}"
    headers = {
        "ETag": etag,
        # Segment bytes never change; private because it is one user's data.
        "Cache-Control": "private, max-age=31536000, immutable",
        "Content-Disposition": f'inline; filename="{filename}"',
    }

    if etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)

    if segment.object_key is None:
        # Inline: the payload is small by contract — no ranges, one body.
        return Response(
            content=json.dumps(segment.payload).encode(),
            media_type=segment.content_type,
            headers=headers,
        )

    return await blob_response(
        request,
        key=segment.object_key,
        byte_size=segment.byte_size,
        content_type=segment.content_type,
        etag=etag,
        headers=headers,
    )
