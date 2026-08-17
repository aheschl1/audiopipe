"""The published contract."""

import httpx

EXPECTED_METHODS = {
    "/health": {"get"},
    "/health/ready": {"get"},
    "/stream-schema.json": {"get"},
    "/v1/auth/login": {"post"},
    "/v1/me": {"get"},
    "/v1/sessions": {"get", "post"},
    "/v1/sessions/{session_id}": {"get"},
    "/v1/sessions/{session_id}/artifacts": {"get"},
    "/v1/resources/location/points": {"get"},
    "/v1/resources/location/tracks": {"get"},
    "/v1/sessions/{session_id}/resources": {"get"},
    "/v1/sessions/{session_id}/resources/location/points": {"get"},
    "/v1/sessions/{session_id}/resources/{resource}/media": {"get"},
    "/v1/sessions/{session_id}/end": {"post"},
    "/v1/sessions/{session_id}/label": {"post"},
    "/v1/sessions/{session_id}/playback": {"post"},
    "/v1/sessions/{session_id}/segments": {"get"},
    "/v1/sessions/{session_id}/speakers": {"get"},
    "/v1/sessions/{session_id}/timeline": {"get"},
    "/v1/sessions/{session_id}/transcripts/{artifact_id}": {"post"},
    "/v1/sessions/{session_id}/ab": {"get", "post"},
    "/v1/sessions/{session_id}/ab/votes": {"post"},
    "/v1/ab/results": {"get"},
    "/v1/segments": {"get"},
    "/v1/segments/{segment_id}": {"get"},
    "/v1/segments/{segment_id}/media": {"get"},
    "/v1/search/audio": {"get"},
    "/v1/search/tags": {"get"},
    "/v1/search/tags/labels": {"get"},
    "/v1/search/transcripts": {"get"},
    "/v1/speakers": {"get"},
    "/v1/speakers/cluster-params": {"get", "post"},
    "/v1/speakers/cluster-params/reset": {"post"},
    "/v1/speakers/projection": {"get"},
    "/v1/speakers/recluster": {"post"},
    "/v1/speakers/transcripts": {"get"},
    "/v1/speakers/{speaker_id}": {"get"},
    "/v1/speakers/{speaker_id}/label": {"post"},
    "/v1/speakers/{speaker_id}/members": {"get"},
    "/v1/speakers/{speaker_id}/members/{artifact_id}/reassign": {"post"},
    "/v1/speakers/{speaker_id}/members/{artifact_id}/unpin": {"post"},
    "/v1/speakers/{speaker_id}/merge": {"post"},
    "/v1/speakers/{speaker_id}/similar": {"get"},
    "/v1/speakers/{speaker_id}/transcripts": {"get"},
}


async def test_openapi_publishes_every_route(client: httpx.AsyncClient) -> None:
    spec = (await client.get("/openapi.json")).json()
    assert {path: set(methods) for path, methods in spec["paths"].items()} == EXPECTED_METHODS
    # The streaming websocket cannot appear in OpenAPI; its contract lives in
    # docs/streaming-protocol.md and is exercised by test_stream.py.
    assert "/v1/sessions/{session_id}/stream" not in spec["paths"]


async def test_segment_schema_hides_storage_layout(client: httpx.AsyncClient) -> None:
    """Guards the omission: a re-added column would fail here, not in review."""
    spec = (await client.get("/openapi.json")).json()
    for schema in ("AudioSegmentRead", "LocationSegmentRead"):
        properties = spec["components"]["schemas"][schema]["properties"]
        assert "bucket" not in properties
        assert "object_key" not in properties
        assert "payload" not in properties
        assert "user_id" not in properties
        assert "checksum_sha256" in properties
        assert "resource" in properties


async def test_paged_schemas_are_generated_per_item_type(client: httpx.AsyncClient) -> None:
    spec = (await client.get("/openapi.json")).json()
    assert {"Page_SegmentRead_", "Page_SessionRead_", "Page_TimelineEvent_"} <= set(
        spec["components"]["schemas"]
    )


async def test_timeline_event_union_is_discriminated(client: httpx.AsyncClient) -> None:
    """Guards the forward-compat contract: typed events behind one `type` tag."""
    spec = (await client.get("/openapi.json")).json()
    schemas = spec["components"]["schemas"]

    discriminator = schemas["TimelineEvent"]["discriminator"]
    assert discriminator["propertyName"] == "type"
    assert set(discriminator["mapping"]) == {
        "session-start",
        "session-end",
        "speech-start",
        "speech-end",
        "transcript",
        "audio-tag",
        "sound-span",
        "location-point",
    }
    assert "label" in schemas["SoundSpanEvent"]["properties"]
    # Transcripts never leak the storage layout; the full text is inline and
    # carries its diarized speaker (no truncation — transcripts are row-only).
    assert "bucket" not in schemas["TranscriptEvent"]["properties"]
    assert "object_key" not in schemas["TranscriptEvent"]["properties"]
    assert "speaker" in schemas["TranscriptEvent"]["properties"]
    assert "truncated" not in schemas["TranscriptEvent"]["properties"]


async def test_docs_render(client: httpx.AsyncClient) -> None:
    assert (await client.get("/docs")).status_code == 200


async def test_stream_schema_matches_models(client: httpx.AsyncClient) -> None:
    """The served schema is exactly what the models produce; codegen targets it."""
    from api.schema.stream_export import build_schema

    served = (await client.get("/stream-schema.json")).json()
    assert served == build_schema()
