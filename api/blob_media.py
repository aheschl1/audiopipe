"""Serving a stored blob over HTTP: ranges, conditional ranges, streaming."""

from collections.abc import AsyncIterator

from fastapi import Request, Response, status
from fastapi.responses import StreamingResponse

from api.ranges import RangeNotSatisfiable, etag_matches, parse_range
from storage.pipe import BlobPipe


async def stream_blob(
    key: str, start: int | None = None, end: int | None = None
) -> AsyncIterator[bytes]:
    """Yield the object's bytes, optionally only the inclusive range.

    The blob store is opened inside the generator: FastAPI closes
    yield-dependencies when the handler returns, which would tear the client
    down before the body is sent.

    The first chunk is pulled here, before the response starts, so a missing
    object raises `BlobNotFoundError` while a 404 is still possible. Left lazy,
    that failure would land mid-body and truncate an already-sent 200.
    """

    async def chunks() -> AsyncIterator[bytes]:
        async with BlobPipe() as blobs:
            async for chunk in blobs.stream(key, start=start, end=end):
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



async def blob_response(
    request: Request,
    *,
    key: str,
    byte_size: int,
    content_type: str,
    etag: str,
    headers: dict[str, str],
) -> Response:
    """The whole object, or the requested ``Range`` of it as a 206."""
    headers = {**headers, "Accept-Ranges": "bytes"}
    # A stale If-Range means the client must take the whole object, not a slice
    # it would splice onto an older copy.
    range_header = request.headers.get("range")
    if_range = request.headers.get("if-range")
    if if_range and not etag_matches(if_range, etag):
        range_header = None

    try:
        byte_range = parse_range(range_header, byte_size)
    except RangeNotSatisfiable:
        return Response(
            status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
            headers={**headers, "Content-Range": f"bytes */{byte_size}"},
        )

    if byte_range is None:
        return StreamingResponse(
            await stream_blob(key),
            media_type=content_type,
            headers={**headers, "Content-Length": str(byte_size)},
        )

    return StreamingResponse(
        await stream_blob(key, byte_range.start, byte_range.end),
        status_code=status.HTTP_206_PARTIAL_CONTENT,
        media_type=content_type,
        headers={
            **headers,
            "Content-Length": str(byte_range.length),
            "Content-Range": byte_range.content_range(byte_size),
        },
    )
