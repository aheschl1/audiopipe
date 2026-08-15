"""The audio endpoint: whole objects, byte ranges and conditional requests."""

import hashlib
from typing import Any

import httpx
import pytest
import pytest_asyncio

from tests.e2e.conftest import TEST_BUCKET, Account, ingest, make_session
from tests.wav import wav_bytes


class Clip:
    """A seeded segment, its URL and the exact bytes that were ingested."""

    def __init__(self, segment: Any, payload: bytes) -> None:
        self.segment = segment
        self.payload = payload
        self.url = f"/v1/segments/{segment.id}/media"
        self.size = len(payload)


@pytest_asyncio.fixture
async def clip(account: Account) -> Clip:
    session = await make_session(account)
    payload = wav_bytes(seconds=0.5)
    return Clip(await ingest(session.id, payload), payload)


async def test_whole_object(client: httpx.AsyncClient, account: Account, clip: Clip) -> None:
    response = await client.get(clip.url, headers=account.headers)

    assert response.status_code == 200
    assert response.content == clip.payload
    assert response.headers["content-length"] == str(clip.size)
    assert response.headers["content-type"] == "audio/wav"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-disposition"].startswith("inline; filename=")
    assert "immutable" in response.headers["cache-control"]


async def test_etag_is_the_stored_checksum(
    client: httpx.AsyncClient, account: Account, clip: Clip
) -> None:
    response = await client.get(clip.url, headers=account.headers)
    assert response.headers["etag"] == f'"{hashlib.sha256(clip.payload).hexdigest()}"'


@pytest.mark.parametrize(
    "header,expected_slice",
    [
        ("bytes=0-99", slice(0, 100)),
        ("bytes=100-199", slice(100, 200)),
        ("bytes=-50", slice(-50, None)),
        ("bytes=0-0", slice(0, 1)),
    ],
)
async def test_ranges_return_exactly_that_slice(
    client: httpx.AsyncClient, account: Account, clip: Clip, header: str, expected_slice: slice
) -> None:
    response = await client.get(clip.url, headers={**account.headers, "Range": header})
    expected = clip.payload[expected_slice]

    assert response.status_code == 206
    assert response.content == expected
    assert response.headers["content-length"] == str(len(expected))


async def test_open_ended_range_runs_to_the_last_byte(
    client: httpx.AsyncClient, account: Account, clip: Clip
) -> None:
    start = clip.size - 44
    response = await client.get(clip.url, headers={**account.headers, "Range": f"bytes={start}-"})

    assert response.status_code == 206
    assert response.content == clip.payload[start:]
    assert response.headers["content-range"] == f"bytes {start}-{clip.size - 1}/{clip.size}"


async def test_range_beyond_the_end_is_clamped(
    client: httpx.AsyncClient, account: Account, clip: Clip
) -> None:
    response = await client.get(clip.url, headers={**account.headers, "Range": "bytes=0-999999"})

    assert response.status_code == 206
    assert response.content == clip.payload
    assert response.headers["content-range"] == f"bytes 0-{clip.size - 1}/{clip.size}"


async def test_unsatisfiable_range_is_416(
    client: httpx.AsyncClient, account: Account, clip: Clip
) -> None:
    headers = {**account.headers, "Range": f"bytes={clip.size + 10}-"}
    response = await client.get(clip.url, headers=headers)

    assert response.status_code == 416
    assert response.headers["content-range"] == f"bytes */{clip.size}"
    assert response.content == b""


@pytest.mark.parametrize("header", ["bytes=abc-def", "bytes=0-9,20-29", "items=0-9", "nonsense"])
async def test_unusable_range_headers_serve_the_whole_object(
    client: httpx.AsyncClient, account: Account, clip: Clip, header: str
) -> None:
    response = await client.get(clip.url, headers={**account.headers, "Range": header})

    assert response.status_code == 200
    assert response.content == clip.payload


async def test_matching_if_none_match_is_304(
    client: httpx.AsyncClient, account: Account, clip: Clip
) -> None:
    etag = (await client.get(clip.url, headers=account.headers)).headers["etag"]

    response = await client.get(clip.url, headers={**account.headers, "If-None-Match": etag})
    assert response.status_code == 304
    assert response.content == b""
    assert response.headers["etag"] == etag


async def test_wildcard_if_none_match_is_304(
    client: httpx.AsyncClient, account: Account, clip: Clip
) -> None:
    response = await client.get(clip.url, headers={**account.headers, "If-None-Match": "*"})
    assert response.status_code == 304


async def test_stale_if_none_match_serves_the_body(
    client: httpx.AsyncClient, account: Account, clip: Clip
) -> None:
    response = await client.get(clip.url, headers={**account.headers, "If-None-Match": '"old"'})
    assert response.status_code == 200
    assert response.content == clip.payload


async def test_matching_if_range_allows_the_slice(
    client: httpx.AsyncClient, account: Account, clip: Clip
) -> None:
    etag = (await client.get(clip.url, headers=account.headers)).headers["etag"]
    headers = {**account.headers, "If-Range": etag, "Range": "bytes=0-9"}

    response = await client.get(clip.url, headers=headers)
    assert response.status_code == 206
    assert response.content == clip.payload[:10]


async def test_stale_if_range_forces_the_whole_object(
    client: httpx.AsyncClient, account: Account, clip: Clip
) -> None:
    headers = {**account.headers, "If-Range": '"stale"', "Range": "bytes=0-9"}

    response = await client.get(clip.url, headers=headers)
    assert response.status_code == 200
    assert response.content == clip.payload


async def test_missing_object_behind_a_live_row_is_404(
    client: httpx.AsyncClient, account: Account, clip: Clip, s3: Any
) -> None:
    """A row pointing at a deleted object must not surface as a 500."""
    s3.delete_object(Bucket=TEST_BUCKET, Key=clip.segment.object_key)

    response = await client.get(clip.url, headers=account.headers)
    assert response.status_code == 404
    assert response.json() == {"detail": "the stored audio for this segment is missing"}


async def test_streaming_is_chunked_for_large_objects(
    client: httpx.AsyncClient, account: Account
) -> None:
    session = await make_session(account)
    payload = wav_bytes(seconds=8)
    segment = await ingest(session.id, payload)

    received = bytearray()
    async with client.stream(
        "GET", f"/v1/segments/{segment.id}/media", headers=account.headers
    ) as response:
        assert response.status_code == 200
        async for chunk in response.aiter_bytes():
            received.extend(chunk)

    assert bytes(received) == payload
