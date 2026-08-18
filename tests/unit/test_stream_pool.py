"""PcmPooler flushing, acking, retries, and the memory cap — fake storage."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from api.config import ApiSettings
from api.schema.stream import AudioFrame
from api.stream_pool import PcmPooler
from database.exceptions import DuplicateSequenceError
from services.wav import WAV_HEADER_BYTES


class FakeTracker:
    def __init__(self, through: int = -1) -> None:
        self.through = through
        self.stored: list[tuple[int, int]] = []
        self.duplicates: list[int] = []

    def note_stored(self, sequence: int, byte_size: int) -> None:
        self.stored.append((sequence, byte_size))

    def note_duplicate(self, sequence: int) -> None:
        self.duplicates.append(sequence)


class InlinePool:
    """Runs submitted stores immediately, like _ChunkPool with one free slot."""

    async def submit(self, store) -> None:
        await store()


class FakeOutbox:
    def __init__(self) -> None:
        self.events = []

    def publish(self, event) -> None:
        self.events.append(event)


def settings(**overrides) -> ApiSettings:
    values = {
        "stream_pool_target_bytes": 100,
        "stream_pool_max_buffer_bytes": 10_000,
        "stream_pool_max_latency_seconds": 60.0,
        "stream_pool_flush_retries": 2,
        "stream_pool_retry_backoff_seconds": 0.01,
    }
    values.update(overrides)
    return ApiSettings(_env_file=None, **values)


def pooler(monkeypatch, store, **overrides) -> tuple[PcmPooler, FakeTracker, FakeOutbox]:
    monkeypatch.setattr("api.stream_pool.stream_segment", store)
    tracker = FakeTracker()
    outbox = FakeOutbox()
    session = SimpleNamespace(id=uuid4(), user_id=uuid4())
    pool = PcmPooler(
        object(),
        session,
        sample_rate_hz=16000,
        channels=1,
        tracker=tracker,
        chunk_pool=InlinePool(),
        outbox=outbox,
        settings=settings(**overrides),
    )
    return pool, tracker, outbox


def frame(sequence: int, size: int = 40) -> AudioFrame:
    return AudioFrame(sequence=sequence, pcm=bytes(size))


class Recorder:
    def __init__(self) -> None:
        self.calls = []

    async def __call__(self, blobs, session, sequence, data, **kwargs):
        self.calls.append((sequence, data, kwargs))


async def test_flushes_at_target_size(monkeypatch) -> None:
    store = Recorder()
    pool, tracker, _ = pooler(monkeypatch, store)
    for sequence in range(3):
        await pool.add(frame(sequence, 40))

    assert len(store.calls) == 1
    sequence, data, kwargs = store.calls[0]
    assert sequence == 2
    assert len(data) == WAV_HEADER_BYTES + 120
    assert kwargs["metadata"] == {"frames": {"first": 0, "last": 2, "count": 3}}
    assert kwargs["content_type"] == "audio/wav"
    assert kwargs["duration_ms"] == round(120 * 1000 / 32000)
    assert tracker.stored == [(0, 40), (1, 40), (2, 40)]
    assert pool.highest_sequence == 2


async def test_captured_at_is_first_frames(monkeypatch) -> None:
    store = Recorder()
    pool, _, _ = pooler(monkeypatch, store)
    first = datetime(2026, 8, 17, tzinfo=UTC)
    await pool.add(AudioFrame(sequence=0, captured_at=first, pcm=bytes(60)))
    await pool.add(AudioFrame(sequence=1, captured_at=datetime.now(UTC), pcm=bytes(60)))
    assert store.calls[0][2]["captured_at"] == first


async def test_latency_timer_flushes(monkeypatch) -> None:
    store = Recorder()
    pool, _, _ = pooler(monkeypatch, store, stream_pool_max_latency_seconds=0.05)
    await pool.add(frame(0, 10))
    assert not store.calls
    await asyncio.sleep(0.15)
    assert len(store.calls) == 1
    pool.shutdown()


async def test_explicit_flush(monkeypatch) -> None:
    store = Recorder()
    pool, tracker, _ = pooler(monkeypatch, store)
    await pool.add(frame(5, 10))
    await pool.flush()
    assert store.calls[0][0] == 5
    assert tracker.stored == [(5, 10)]
    await pool.flush()  # empty pool: no second store
    assert len(store.calls) == 1


async def test_retries_then_succeeds(monkeypatch) -> None:
    attempts = 0

    async def flaky(blobs, session, sequence, data, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("blob store hiccup")

    pool, tracker, outbox = pooler(monkeypatch, flaky)
    for sequence in range(3):
        await pool.add(frame(sequence, 40))
    assert attempts == 2
    assert tracker.stored
    assert not pool.failed
    assert not outbox.events


async def test_retries_exhausted_is_session_fatal(monkeypatch) -> None:
    async def broken(blobs, session, sequence, data, **kwargs):
        raise RuntimeError("blob store down")

    pool, tracker, outbox = pooler(monkeypatch, broken)
    for sequence in range(3):
        await pool.add(frame(sequence, 40))
    assert pool.failed
    assert not tracker.stored
    [event] = outbox.events
    assert event.scope == "session"
    assert event.code == "storage_failure"


async def test_duplicate_sequence_acks_as_duplicates(monkeypatch) -> None:
    async def duplicate(blobs, session, sequence, data, **kwargs):
        raise DuplicateSequenceError(session.id, sequence)

    pool, tracker, _ = pooler(monkeypatch, duplicate)
    for sequence in range(3):
        await pool.add(frame(sequence, 40))
    assert tracker.duplicates == [0, 1, 2]
    assert not tracker.stored
    assert not pool.failed


async def test_buffer_cap_blocks_add(monkeypatch) -> None:
    release = asyncio.Event()

    async def slow(blobs, session, sequence, data, **kwargs):
        await release.wait()

    pool, _, _ = pooler(
        monkeypatch, slow, stream_pool_target_bytes=40, stream_pool_max_buffer_bytes=100
    )

    async def feed() -> None:
        for sequence in range(5):  # 200 bytes, double the cap
            await pool.add(frame(sequence, 40))

    feeder = asyncio.create_task(feed())
    await asyncio.sleep(0.05)
    assert not feeder.done()  # blocked on the cap, not looping on
    release.set()
    await asyncio.wait_for(feeder, timeout=1.0)
