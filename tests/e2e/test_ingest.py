"""`services.segments`: the two stores kept consistent with each other.

Object state is checked with an independent boto3 client rather than through the
code under test.
"""

import asyncio
import hashlib
from typing import Any
from uuid import uuid4

import pytest

from database.exceptions import NotFoundError
from database.pipe import DatabasePipe
from services import segments as segment_service
from storage.keys import session_prefix
from tests.e2e.conftest import TEST_BUCKET, Account, ingest, make_session
from tests.wav import wav_bytes


def keys_under(s3: Any, prefix: str = "") -> list[str]:
    page = s3.list_objects_v2(Bucket=TEST_BUCKET, Prefix=prefix)
    return [obj["Key"] for obj in page.get("Contents", [])]


async def test_ingest_records_size_and_checksum(account: Account, s3: Any) -> None:
    session = await make_session(account)
    payload = wav_bytes(seconds=0.4)

    segment = await ingest(session.id, payload)

    assert segment.byte_size == len(payload)
    assert segment.checksum_sha256 == hashlib.sha256(payload).digest()
    stored = s3.get_object(Bucket=TEST_BUCKET, Key=segment.object_key)["Body"].read()
    assert stored == payload


async def test_object_key_carries_user_session_and_sequence(account: Account) -> None:
    session = await make_session(account)
    segment = await ingest(session.id, wav_bytes())

    assert segment.object_key.startswith(session_prefix(account.user.id, session.id))
    assert segment.object_key.endswith(".wav")
    assert f"{segment.sequence:06d}" in segment.object_key


async def test_concurrent_ingests_take_distinct_sequences(
    account: Account, s3: Any
) -> None:
    """No lock spans the upload; the unique constraint arbitrates the race.

    Losers retry with a fresh sequence read and delete their provisional
    object, so the store ends up with exactly one object per row.
    """
    session = await make_session(account)
    payloads = [wav_bytes(seconds=0.05, freq=300 + 10 * i) for i in range(4)]

    segments = await asyncio.gather(
        *(ingest(session.id, payload) for payload in payloads)
    )

    assert sorted(segment.sequence for segment in segments) == [0, 1, 2, 3]
    stored = keys_under(s3, session_prefix(account.user.id, session.id))
    assert sorted(stored) == sorted(segment.object_key for segment in segments)


async def test_failed_ingest_leaves_no_orphan_object(account: Account, s3: Any) -> None:
    before = keys_under(s3)

    with pytest.raises(NotFoundError):
        await segment_service.ingest_segment(uuid4(), wav_bytes(), content_type="audio/wav")

    assert keys_under(s3) == before


async def test_read_segment_round_trips(account: Account) -> None:
    session = await make_session(account)
    payload = wav_bytes(seconds=0.25)
    segment = await ingest(session.id, payload)

    fetched, data = await segment_service.read_segment(segment.id)
    assert fetched.id == segment.id
    assert data == payload


async def test_presigned_url_is_issued_for_the_object(account: Account) -> None:
    session = await make_session(account)
    segment = await ingest(session.id, wav_bytes())

    url = await segment_service.segment_url(segment.id, expires_in=60)
    assert segment.object_key in url
    assert "X-Amz-Signature" in url


async def test_delete_segment_removes_row_and_object(account: Account, s3: Any) -> None:
    session = await make_session(account)
    segment = await ingest(session.id, wav_bytes())

    assert await segment_service.delete_segment(segment.id) is True

    async with DatabasePipe() as pipe:
        assert await pipe.segments.get(segment.id) is None
    assert keys_under(s3, segment.object_key) == []


async def test_delete_segment_reports_unknown_ids(account: Account) -> None:
    assert await segment_service.delete_segment(uuid4()) is False


async def test_delete_session_clears_rows_and_the_whole_prefix(
    account: Account, s3: Any
) -> None:
    session = await make_session(account)
    for freq in (440, 660):
        await ingest(session.id, wav_bytes(freq=freq))

    removed = await segment_service.delete_session(session.id)

    assert removed == 2
    assert keys_under(s3, session_prefix(account.user.id, session.id)) == []
    async with DatabasePipe() as pipe:
        assert await pipe.sessions.get(session.id) is None
        assert await pipe.segments.list_for_user(account.user.id) == []


async def test_deleting_a_user_directly_orphans_objects(account: Account, s3: Any) -> None:
    """The documented gap: cascades reach rows, never blobs."""
    session = await make_session(account)
    segment = await ingest(session.id, wav_bytes())

    async with DatabasePipe() as pipe:
        await pipe.users.delete(account.user.id)

    assert keys_under(s3, segment.object_key) == [segment.object_key]


async def test_ingest_inline_location_writes_row_only(account: Account, s3: Any) -> None:
    """An inline resource is a single row: no object, derived batch span."""
    import json

    session = await make_session(account)
    points = [
        {"lat": 51.0, "lon": -114.0, "t": 10_000},
        {"lat": 51.1, "lon": -114.1, "t": 40_000},
    ]
    segment = await segment_service.ingest_segment(
        session.id, json.dumps({"points": points}).encode(), resource="location"
    )

    assert segment.resource == "location"
    assert segment.bucket is None and segment.object_key is None
    assert segment.payload == {"points": points}
    assert segment.duration_ms == 30_000
    assert segment.captured_at is not None
    assert keys_under(s3, session_prefix(account.user.id, session.id)) == []

    with pytest.raises(segment_service.NoBlobError):
        await segment_service.read_segment(segment.id)
    with pytest.raises(segment_service.NoBlobError):
        await segment_service.segment_url(segment.id)

    # Deleting an inline segment touches no objects and still removes the row.
    assert await segment_service.delete_segment(segment.id) is True
    async with DatabasePipe() as pipe:
        assert await pipe.segments.get(segment.id) is None


async def test_ingest_rejects_invalid_location_before_writing(account: Account) -> None:
    session = await make_session(account)
    from resources import ResourceValidationError

    with pytest.raises(ResourceValidationError):
        await segment_service.ingest_segment(session.id, b"not json", resource="location")
    async with DatabasePipe() as pipe:
        assert await pipe.segments.list_for_session(session.id) == []
