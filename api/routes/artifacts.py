"""Pipeline artifacts' stored bytes (thumbnails, video proxies, ...)."""

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse

from api.blob_media import blob_response
from api.deps import OwnedArtifact
from api.ranges import etag_matches

router = APIRouter(prefix="/artifacts", tags=["artifacts"])


@router.get(
    "/{artifact_id}/media",
    summary="Download an artifact's stored blob",
    response_class=StreamingResponse,
    responses={
        200: {"content": {"image/jpeg": {}, "video/mp4": {}}, "description": "The blob."},
        206: {"content": {"video/mp4": {}}, "description": "The requested byte range."},
        304: {"description": "The client's cached copy is still current."},
        404: {"description": "Not yours, or the artifact stores no servable blob."},
        416: {"description": "The requested range lies outside the object."},
    },
)
async def get_artifact_media(artifact: OwnedArtifact, request: Request) -> Response:
    """For artifacts that record their blob's ``content_type`` and ``byte_size``
    in metadata — the visual pipelines' thumbnails and proxies. Browsers
    authenticate `<img>`/`<video>` with the sign-in cookie."""
    content_type = artifact.metadata.get("content_type")
    byte_size = artifact.metadata.get("byte_size")
    if artifact.object_key is None or not content_type or not isinstance(byte_size, int):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no servable blob")
    etag = f'"{artifact.id}-{byte_size}"'
    headers = {
        "ETag": etag,
        "Cache-Control": "private, max-age=31536000, immutable",
        "Content-Disposition": "inline",
    }
    if etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    return await blob_response(
        request,
        key=artifact.object_key,
        byte_size=byte_size,
        content_type=content_type,
        etag=etag,
        headers=headers,
    )
