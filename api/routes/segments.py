"""Resource segments: metadata, and the stored bytes themselves. Read-only."""

import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import StreamingResponse

from api.deps import CurrentUser, OwnedSegment, Paging, Pipe
from api.ranges import RangeNotSatisfiable, etag_matches, parse_range
from api.schema.common import Page
from api.schema.segments import SegmentRead, segment_read
from database.schema.segments import ResourceSegment
from storage.keys import suffix_for
from storage.pipe import BlobPipe

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


async def _stream_blob(
    segment: ResourceSegment, start: int | None = None, end: int | None = None
) -> AsyncIterator[bytes]:
    """Yield the segment's bytes, optionally only the inclusive range.

    The blob store is opened inside the generator: FastAPI closes
    yield-dependencies when the handler returns, which would tear the client
    down before the body is sent.

    The first chunk is pulled here, before the response starts, so a missing
    object raises `BlobNotFoundError` while a 404 is still possible. Left lazy,
    that failure would land mid-body and truncate an already-sent 200.
    """

    async def chunks() -> AsyncIterator[bytes]:
        async with BlobPipe() as blobs:
            async for chunk in blobs.stream(segment.object_key, start=start, end=end):
                yield chunk

    stream = chunks()
    try:
        first = await anext(stream)
    except StopAsyncIteration:
        first = b""

    async def body() -> AsyncIterator[bytes]:
        yield first
        async for chunk in stream:
            yield chunk

    return body()


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
async def get_segment_media(segment: OwnedSegment, request: Request) -> Response:
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

    headers["Accept-Ranges"] = "bytes"
    # A stale If-Range means the client must take the whole object, not a slice
    # it would splice onto an older copy.
    range_header = request.headers.get("range")
    if_range = request.headers.get("if-range")
    if if_range and not etag_matches(if_range, etag):
        range_header = None

    try:
        byte_range = parse_range(range_header, segment.byte_size)
    except RangeNotSatisfiable:
        return Response(
            status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
            headers={**headers, "Content-Range": f"bytes */{segment.byte_size}"},
        )

    if byte_range is None:
        return StreamingResponse(
            await _stream_blob(segment),
            media_type=segment.content_type,
            headers={**headers, "Content-Length": str(segment.byte_size)},
        )

    return StreamingResponse(
        await _stream_blob(segment, byte_range.start, byte_range.end),
        status_code=status.HTTP_206_PARTIAL_CONTENT,
        media_type=segment.content_type,
        headers={
            **headers,
            "Content-Length": str(byte_range.length),
            "Content-Range": byte_range.content_range(segment.byte_size),
        },
    )
