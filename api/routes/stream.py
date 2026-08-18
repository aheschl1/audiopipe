"""The streaming upload websocket.

Implements the protocol in docs/streaming-protocol.md: a client attaches to an
open session it owns, sends audio as binary frames — v2 raw PCM frames pooled
into stored segments and tapped into the live layer, or v1 self-contained
chunks stored 1:1 — and receives typed JSON events (cumulative acks, errors,
and effect output) back.

None of the application's HTTP machinery applies here — no global exception
handlers, no ``HTTPBearer`` — so this module authenticates the upgrade request
itself and maps every failure to a denial response, an error event, or a close
code.
"""

import asyncio
import logging
from uuid import UUID

from fastapi import APIRouter, WebSocket
from fastapi.responses import JSONResponse

from api.config import ApiSettings, get_settings
from api.deps import authenticate_token
from api.schema.common import ErrorResponse
from api.schema.stream import (
    SUPPORTED_VERSIONS,
    Ack,
    AckWindow,
    AudioFrame,
    Bye,
    ChunkHeader,
    EffectEvent,
    Finish,
    FrameError,
    Hello,
    Rotate,
    ServerEvent,
    StreamError,
    StreamLimits,
    Welcome,
    decode_chunk,
    decode_v2_frame,
    parse_client_frame,
)
from api.stream_pool import PcmPooler
import resources
from live import registry as live_registry
from live.tap import LiveSessionHandle
from database.exceptions import DuplicateSequenceError, NotFoundError, SessionEndedError
from database.pipe import DatabasePipe
from database.schema.sessions import RecordingSession
from resources import Resource, ResourceValidationError
from services.segments import ChecksumMismatchError, stream_segment
from storage.pipe import BlobPipe
from storage.s3 import S3BlobStore

logger = logging.getLogger(__name__)

router = APIRouter(tags=["stream"])

# Application close codes. With header auth, handshake failures are HTTP
# statuses on the rejected upgrade; in hello-token mode they are close codes.
CLOSE_PROTOCOL_ERROR = 4400
CLOSE_SESSION_NOT_FOUND = 4404
CLOSE_SESSION_ENDED = 4409
CLOSE_INTERNAL = 4500


class _Outbox:
    """Serializes server events onto the socket through one writer task.

    Acks, errors, and any future effect producer publish here rather than
    writing to the socket directly, so concurrent senders cannot interleave.
    A send failure marks the socket dead and later events are drained silently.
    """

    def __init__(self, websocket: WebSocket) -> None:
        self._websocket = websocket
        self._queue: asyncio.Queue[ServerEvent | None] = asyncio.Queue()
        self._task = asyncio.create_task(self._run())
        self._closed = False
        self._dead = False

    def publish(self, event: ServerEvent) -> None:
        if not self._closed:
            self._queue.put_nowait(event)

    async def _run(self) -> None:
        while (event := await self._queue.get()) is not None:
            if self._dead:
                continue
            try:
                await self._websocket.send_text(event.model_dump_json(exclude_none=True))
            except Exception:
                self._dead = True

    async def close(self) -> None:
        """Flush everything published so far and stop the writer."""
        if not self._closed:
            self._closed = True
            self._queue.put_nowait(None)
        await self._task

    def abort(self) -> None:
        """Drop the writer without flushing; for cancellation paths only."""
        self._closed = True
        self._dead = True
        self._task.cancel()


class _AckTracker:
    """Cumulative-ack bookkeeping and the N-chunks / T-seconds window."""

    def __init__(self, outbox: _Outbox, base: int, window: AckWindow) -> None:
        self._outbox = outbox
        self.through = base  # highest sequence with no gap below it
        self._window = window
        self._stored: set[int] = set()  # stored above `through` (gaps below them)
        self._count = 0
        self._bytes = 0
        self._duplicates: list[int] = []
        self._timer: asyncio.Task | None = None

    def note_stored(self, sequence: int, byte_size: int) -> None:
        self._count += 1
        self._bytes += byte_size
        self._advance(sequence)
        self._on_activity()

    def note_duplicate(self, sequence: int) -> None:
        # The database says the row exists, so it counts toward contiguity
        # even though nothing was written now.
        self._duplicates.append(sequence)
        self._advance(sequence)
        self._on_activity()

    def _advance(self, sequence: int) -> None:
        self._stored.add(sequence)
        while self.through + 1 in self._stored:
            self.through += 1
            self._stored.discard(self.through)

    def _on_activity(self) -> None:
        if self._count + len(self._duplicates) >= self._window.chunks:
            self.flush()
        elif self._timer is None:
            self._timer = asyncio.create_task(self._flush_later())

    async def _flush_later(self) -> None:
        await asyncio.sleep(self._window.seconds)
        self._timer = None
        self.flush()

    def flush(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self._count == 0 and not self._duplicates:
            return
        self._outbox.publish(
            Ack(
                through=self.through,
                count=self._count,
                bytes=self._bytes,
                # Sorted: stores complete in arbitrary order, and the set is
                # what matters — keep the wire deterministic anyway.
                duplicates=tuple(sorted(self._duplicates)),
            )
        )
        self._count = 0
        self._bytes = 0
        self._duplicates = []

    def shutdown(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None


def _bearer_token(websocket: WebSocket) -> str | None:
    scheme, _, token = (websocket.headers.get("authorization") or "").partition(" ")
    token = token.strip()
    return token if scheme.lower() == "bearer" and token else None


async def _deny(websocket: WebSocket, status_code: int, detail: str) -> None:
    """Reject the upgrade with a real HTTP response where the server allows it."""
    if "websocket.http.response" in websocket.scope.get("extensions", {}):
        await websocket.send_denial_response(
            JSONResponse(status_code=status_code, content=ErrorResponse(detail=detail).model_dump())
        )
    else:
        # Servers without the denial extension surface this as a plain 403.
        await websocket.close(code=1008, reason=detail)


class _Attachment:
    """The outcome of authenticating and loading the target session."""

    def __init__(
        self, session: RecordingSession | None, next_sequence: int, failure: str | None
    ) -> None:
        self.session = session
        self.next_sequence = next_sequence
        self.failure = failure  # None, or a key into _DENIALS / _HELLO_CLOSE_CODES


_DENIALS = {
    "unauthorized": (401, "a valid bearer token is required"),
    "not_found": (404, "recording session not found"),
    "ended": (409, "recording session has ended"),
}

# In hello-token mode the socket is already accepted, so failures are close
# codes instead of HTTP statuses.
_HELLO_CLOSE_CODES = {
    "unauthorized": 1008,
    "not_found": CLOSE_SESSION_NOT_FOUND,
    "ended": CLOSE_SESSION_ENDED,
}


async def _attach(session_id: UUID, token: str | None) -> _Attachment:
    """Authenticate the token and load + check the session it targets."""
    async with DatabasePipe() as pipe:
        user = await authenticate_token(pipe, token)
        if user is None:
            return _Attachment(None, 0, "unauthorized")
        session = await pipe.sessions.get(session_id)
        if session is None or session.user_id != user.id:
            return _Attachment(None, 0, "not_found")
        if not session.is_open:
            return _Attachment(None, 0, "ended")
        # Attaching counts as activity; briefly locks the session row.
        await pipe.sessions.touch(session.id)
        next_sequence = await pipe.segments.next_sequence(session.id)
    return _Attachment(session, next_sequence, None)


@router.websocket("/sessions/{session_id}/stream")
async def stream_session_audio(websocket: WebSocket, session_id: UUID) -> None:
    """Attach to an open session and ingest streamed chunks."""
    settings = get_settings()

    header_token = _bearer_token(websocket)
    if header_token is not None:
        # Canonical mode: authenticate before accepting, so failures are real
        # HTTP statuses on the rejected upgrade. Any hello.token is ignored.
        attachment = await _attach(session_id, header_token)
        if attachment.failure is not None:
            status_code, detail = _DENIALS[attachment.failure]
            await _deny(websocket, status_code, detail)
            return
        await websocket.accept()
        hello, close_code = await _await_hello(websocket, settings)
    else:
        # Fallback mode for clients whose websocket cannot set headers: accept
        # first, authenticate the token carried inside hello.
        await websocket.accept()
        hello, close_code = await _await_hello(websocket, settings)
        attachment = None
        if hello is not None:
            attachment = await _attach(session_id, hello.token)
            if attachment.failure is not None:
                _, detail = _DENIALS[attachment.failure]
                await websocket.close(
                    code=_HELLO_CLOSE_CODES[attachment.failure], reason=detail
                )
                return

    if hello is None or attachment is None:
        if close_code is not None:
            await websocket.close(code=close_code)
        return

    session = attachment.session
    assert session is not None  # failure was None
    next_sequence = attachment.next_sequence

    outbox = _Outbox(websocket)
    window = AckWindow(
        chunks=settings.stream_ack_window_chunks, seconds=settings.stream_ack_window_seconds
    )
    tracker = _AckTracker(outbox, next_sequence - 1, window)
    close_code = None

    # The live tap is best-effort and v2-only: every audio frame is forwarded
    # while live is enabled; `effects` only selects which pipelines run and
    # whose events come back.
    tap = getattr(websocket.app.state, "live_tap", None)
    live_handle = None
    effects: tuple[str, ...] = ()
    if tap is not None and hello.version >= 2:
        effects = tuple(name for name in hello.effects if name in live_registry.names())
        live_handle = tap.attach(
            session_id=session.id,
            user_id=session.user_id,
            sample_rate_hz=hello.defaults.sample_rate_hz,
            channels=hello.defaults.channels,
            effects=effects,
            on_event=lambda event: outbox.publish(
                EffectEvent(
                    effect=event.effect,
                    event=event.event,
                    sequence=event.sequence,
                    data=event.data,
                )
            ),
        )
    try:
        outbox.publish(
            Welcome(
                version=hello.version,
                session_id=session.id,
                next_sequence=next_sequence,
                ack_window=window,
                limits=StreamLimits(
                    max_chunk_bytes=settings.stream_max_chunk_bytes,
                    max_audio_frame_bytes=(
                        settings.stream_max_audio_frame_bytes if hello.version >= 2 else None
                    ),
                ),
                effects=effects,
                resources=resources.names(),
            )
        )
        async with BlobPipe() as blobs:
            bye_reason, close_code = await _pump(
                websocket, outbox, tracker, blobs, session, hello, settings, live_handle
            )
        if bye_reason is not None:
            outbox.publish(Bye(reason=bye_reason, through=tracker.through))
    except asyncio.CancelledError:
        # Server shutdown: uvicorn cancelled this handler. Say goodbye if the
        # socket still works, but never delay the shutdown for it.
        outbox.publish(Bye(reason="shutdown", through=tracker.through))
        try:
            async with asyncio.timeout(1.0):
                await outbox.close()
                await websocket.close(code=1001)
        except BaseException:
            outbox.abort()
        raise
    except Exception:
        logger.exception("stream for session %s failed", session.id)
        outbox.publish(
            StreamError(scope="session", code="internal", detail="internal error")
        )
        close_code = CLOSE_INTERNAL
    finally:
        if live_handle is not None:
            live_handle.detach()
        tracker.shutdown()
        try:
            await outbox.close()
        except asyncio.CancelledError:
            outbox.abort()
            raise
        if close_code is not None:
            try:
                await websocket.close(code=close_code)
            except RuntimeError:
                pass  # already closed under us


async def _await_hello(
    websocket: WebSocket, settings: ApiSettings
) -> tuple[Hello | None, int | None]:
    """The AWAITING_HELLO state: one text frame, on time, of the right type."""
    try:
        message = await asyncio.wait_for(
            websocket.receive(), timeout=settings.stream_hello_timeout_seconds
        )
    except TimeoutError:
        return None, CLOSE_PROTOCOL_ERROR
    if message["type"] == "websocket.disconnect":
        return None, None
    text = message.get("text")
    if text is None:
        return None, CLOSE_PROTOCOL_ERROR
    try:
        frame = parse_client_frame(text)
    except FrameError:
        return None, CLOSE_PROTOCOL_ERROR
    if not isinstance(frame, Hello) or frame.version not in SUPPORTED_VERSIONS:
        return None, CLOSE_PROTOCOL_ERROR
    if frame.version >= 2 and (
        frame.defaults.codec != "pcm_s16le"
        or frame.defaults.sample_rate_hz is None
        or frame.defaults.channels is None
    ):
        # v2 audio frames are raw PCM; without these the payloads mean nothing.
        return None, CLOSE_PROTOCOL_ERROR
    return frame, None


class _ChunkPool:
    """Runs chunk stores concurrently, bounded by a semaphore.

    ``submit`` blocks on the semaphore when every slot is busy, so the receive
    loop stops reading frames and backpressure lands in TCP instead of an
    unbounded task pile. Completion order does not matter: the ack tracker's
    contiguity set already handles gaps, and the ``(session, sequence)``
    uniqueness arbitrates a retransmit racing its original.

    ``_ingest_chunk`` reports every recoverable problem itself, so a task ends
    in one of three ways: clean, session-fatal (recorded in ``fatal``), or an
    unexpected crash (recorded in ``failure`` for the pump to re-raise).
    """

    def __init__(self, limit: int) -> None:
        self._slots = asyncio.Semaphore(limit)
        self._tasks: set[asyncio.Task] = set()
        self.fatal: Exception | None = None  # SessionEndedError / NotFoundError
        self.failure: BaseException | None = None

    @property
    def settled(self) -> bool:
        return self.fatal is not None or self.failure is not None

    async def submit(self, store) -> None:
        """``store`` is a zero-argument callable returning the coroutine, so a
        cancellation while waiting for a slot leaves nothing half-created."""
        await self._slots.acquire()
        task = asyncio.create_task(self._run(store))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, store) -> None:
        try:
            await store()
        except (SessionEndedError, NotFoundError) as exc:
            if self.fatal is None:
                self.fatal = exc
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            if self.failure is None:
                self.failure = exc
        finally:
            self._slots.release()

    async def drain(self) -> None:
        """Wait for every in-flight store to finish (outcomes land in flags)."""
        while self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    def cancel_all(self) -> None:
        for task in self._tasks:
            task.cancel()


async def _pump(
    websocket: WebSocket,
    outbox: _Outbox,
    tracker: _AckTracker,
    blobs: S3BlobStore,
    session: RecordingSession,
    hello: Hello,
    settings: ApiSettings,
    live_handle: LiveSessionHandle | None = None,
) -> tuple[str | None, int | None]:
    """The STREAMING state. Returns (bye reason, close code) for the teardown."""
    pool = _ChunkPool(settings.stream_ingest_concurrency)
    pooler = None
    if hello.version >= 2:
        # _await_hello guaranteed the PCM parameters are present.
        pooler = PcmPooler(
            blobs,
            session,
            sample_rate_hz=hello.defaults.sample_rate_hz,
            channels=hello.defaults.channels,
            tracker=tracker,
            chunk_pool=pool,
            outbox=outbox,
            settings=settings,
        )
    try:
        return await _pump_frames(
            websocket, outbox, tracker, blobs, session, hello, settings, pool, pooler,
            live_handle,
        )
    except asyncio.CancelledError:
        pool.cancel_all()
        raise
    finally:
        if pooler is not None:
            pooler.shutdown()
        # Every return path drained already; this covers the exception paths.
        await pool.drain()


async def _session_state(session_id: UUID) -> RecordingSession | None:
    async with DatabasePipe() as pipe:
        return await pipe.sessions.get(session_id)


async def _ended_teardown(
    outbox: _Outbox, tracker: _AckTracker, session_id: UUID
) -> tuple[str | None, int | None]:
    """The session ended under this stream; tell the client and close.

    A ``rotated`` marker means the end was a planned split: a ``rotate``
    event precedes the error so a current client reopens immediately, while
    one that predates the event ignores it and handles the close it already
    knows. A plain end (REST, sweeper, deletion) stays a bare
    ``session_ended`` — an explicit stop must not restart recording.
    """
    tracker.flush()
    current = await _session_state(session_id)
    if current is not None and current.metadata.get("rotated"):
        outbox.publish(Rotate(through=tracker.through))
    outbox.publish(
        StreamError(scope="session", code="session_ended", detail="the session has ended")
    )
    return None, CLOSE_SESSION_ENDED


async def _settle(
    pool: _ChunkPool, outbox: _Outbox, tracker: _AckTracker, session_id: UUID
) -> tuple[str | None, int | None] | None:
    """Drain the pool and translate what its tasks reported.

    None means all clear; a tuple is the pump's session-fatal return. An
    unexpected task crash re-raises here, into the handler's internal-error
    path — exactly where it landed when stores ran inline.
    """
    await pool.drain()
    if pool.failure is not None:
        raise pool.failure
    if pool.fatal is not None:
        return await _ended_teardown(outbox, tracker, session_id)
    return None


async def _pump_frames(
    websocket: WebSocket,
    outbox: _Outbox,
    tracker: _AckTracker,
    blobs: S3BlobStore,
    session: RecordingSession,
    hello: Hello,
    settings: ApiSettings,
    pool: _ChunkPool,
    pooler: PcmPooler | None,
    live_handle: LiveSessionHandle | None = None,
) -> tuple[str | None, int | None]:
    loop = asyncio.get_running_loop()
    idle_deadline = loop.time() + settings.stream_idle_timeout_seconds
    while True:
        if pool.settled and (outcome := await _settle(pool, outbox, tracker, session.id)) is not None:
            return outcome
        if pooler is not None and pooler.failed:
            # The pooler already published the session-scoped storage_failure.
            await pool.drain()
            return None, CLOSE_INTERNAL

        # Receive in short slices so a quiet stream still notices its session
        # being split (or otherwise ended) within the check interval, not only
        # on its next chunk or at the idle timeout.
        timeout = min(settings.stream_session_check_seconds, idle_deadline - loop.time())
        try:
            message = await asyncio.wait_for(websocket.receive(), timeout=max(timeout, 0.0))
        except TimeoutError:
            if (outcome := await _settle(pool, outbox, tracker, session.id)) is not None:
                return outcome
            if loop.time() >= idle_deadline:
                if pooler is not None:
                    await pooler.flush()
                    if (outcome := await _settle(pool, outbox, tracker, session.id)) is not None:
                        return outcome
                tracker.flush()
                return "idle", 1000
            current = await _session_state(session.id)
            if current is None or current.ended_at is not None:
                return await _ended_teardown(outbox, tracker, session.id)
            continue

        idle_deadline = loop.time() + settings.stream_idle_timeout_seconds
        if message["type"] == "websocket.disconnect":
            # In-flight stores finish and count; the session stays open for a
            # reconnect, and the sweeper ends it if none arrives in time.
            if pooler is not None:
                await pooler.flush()
            await pool.drain()
            if pool.failure is not None:
                raise pool.failure
            return None, None

        data = message.get("bytes")
        if data is not None:
            if pooler is None:
                await pool.submit(
                    lambda data=data: _ingest_chunk(
                        outbox, tracker, blobs, session, hello, settings, data
                    )
                )
            else:
                await _ingest_v2_frame(
                    outbox, tracker, blobs, session, hello, settings, pool, pooler,
                    live_handle, data,
                )
            continue

        try:
            frame = parse_client_frame(message.get("text") or "")
        except FrameError as exc:
            await pool.drain()
            outbox.publish(
                StreamError(scope="session", code="protocol_error", detail=str(exc))
            )
            return None, CLOSE_PROTOCOL_ERROR
        if isinstance(frame, Finish):
            if pooler is not None:
                await pooler.flush()
            if (outcome := await _settle(pool, outbox, tracker, session.id)) is not None:
                return outcome
            if pooler is not None and pooler.failed:
                await pool.drain()
                return None, CLOSE_INTERNAL
            tracker.flush()
            async with DatabasePipe() as pipe:
                await pipe.sessions.end(session.id, frame.ended_at)
            return "finished", 1000
        await pool.drain()
        outbox.publish(
            StreamError(scope="session", code="protocol_error", detail="unexpected hello")
        )
        return None, CLOSE_PROTOCOL_ERROR


async def _ingest_v2_frame(
    outbox: _Outbox,
    tracker: _AckTracker,
    blobs: S3BlobStore,
    session: RecordingSession,
    hello: Hello,
    settings: ApiSettings,
    pool: _ChunkPool,
    pooler: PcmPooler,
    live_handle: LiveSessionHandle | None,
    data: bytes,
) -> None:
    """Route one v2 binary frame: audio into the pooler, envelopes as chunks."""
    try:
        decoded = decode_v2_frame(data)
    except FrameError as exc:
        outbox.publish(StreamError(scope="chunk", code="bad_frame", detail=str(exc)))
        return
    if not isinstance(decoded, AudioFrame):
        header, payload = decoded
        await pool.submit(
            lambda: _ingest_decoded(
                outbox, tracker, blobs, session, hello, settings, header, payload
            )
        )
        return

    def reject(code: str, detail: str) -> None:
        outbox.publish(
            StreamError(scope="chunk", code=code, detail=detail, sequence=decoded.sequence)
        )

    sample_width = hello.defaults.channels * 2
    if not decoded.pcm or len(decoded.pcm) % sample_width:
        reject("bad_frame", f"payload must be a non-empty multiple of {sample_width} bytes")
        return
    if len(decoded.pcm) > settings.stream_max_audio_frame_bytes:
        reject(
            "frame_too_large",
            f"payload of {len(decoded.pcm)} bytes exceeds {settings.stream_max_audio_frame_bytes}",
        )
        return
    if decoded.sequence <= pooler.highest_sequence:
        if decoded.sequence <= tracker.through:
            tracker.note_duplicate(decoded.sequence)
        else:
            reject("bad_sequence", "audio frame sequences must be strictly increasing")
        return
    if live_handle is not None:
        # Before the pooler: the live path must see the frame even when the
        # durable path is applying backpressure.
        live_handle.send_frame(decoded.sequence, decoded.pcm)
    await pooler.add(decoded)


async def _ingest_chunk(
    outbox: _Outbox,
    tracker: _AckTracker,
    blobs: S3BlobStore,
    session: RecordingSession,
    hello: Hello,
    settings: ApiSettings,
    data: bytes,
) -> None:
    """Store one v1 chunk, reporting recoverable problems as chunk-scoped errors."""
    try:
        header, payload = decode_chunk(data)
    except FrameError as exc:
        outbox.publish(StreamError(scope="chunk", code="bad_header", detail=str(exc)))
        return
    await _ingest_decoded(outbox, tracker, blobs, session, hello, settings, header, payload)


async def _ingest_decoded(
    outbox: _Outbox,
    tracker: _AckTracker,
    blobs: S3BlobStore,
    session: RecordingSession,
    hello: Hello,
    settings: ApiSettings,
    header: ChunkHeader,
    payload: bytes,
) -> None:
    """Store one decoded enveloped chunk (the v1 path, and v2 envelopes)."""

    def reject(code: str, detail: str, sequence: int | None = None) -> None:
        outbox.publish(
            StreamError(scope="chunk", code=code, detail=detail, sequence=sequence)
        )

    if not payload:
        reject("bad_header", "empty payload", header.sequence)
        return
    if len(payload) > settings.stream_max_chunk_bytes:
        reject(
            "chunk_too_large",
            f"payload of {len(payload)} bytes exceeds {settings.stream_max_chunk_bytes}",
            header.sequence,
        )
        return
    expected_checksum: bytes | None = None
    if header.checksum_sha256 is not None:
        try:
            expected_checksum = bytes.fromhex(header.checksum_sha256)
        except ValueError:
            reject("bad_header", "checksum_sha256 is not hex", header.sequence)
            return
    if header.resource not in resources.names():
        # An invalid header field value, same as any other; only a client
        # newer than the server can trigger it.
        reject("bad_header", f"unknown resource {header.resource!r}", header.sequence)
        return

    # The hello defaults are PCM parameters — they apply to audio chunks only.
    defaults = hello.defaults
    if header.resource == Resource.AUDIO:
        content_type = header.content_type or defaults.content_type
        attrs = {
            "codec": defaults.codec,
            "sample_rate_hz": defaults.sample_rate_hz,
            "channels": defaults.channels,
        }
    else:
        content_type = header.content_type
        attrs = {}
    try:
        segment = await stream_segment(
            blobs,
            session,
            header.sequence,
            payload,
            resource=header.resource,
            content_type=content_type,
            captured_at=header.captured_at,
            duration_ms=header.duration_ms,
            attrs=attrs,
            metadata=header.metadata,
            expected_checksum=expected_checksum,
        )
    except ChecksumMismatchError as exc:
        reject("checksum_mismatch", str(exc), header.sequence)
        return
    except ResourceValidationError as exc:
        # Recoverable like checksum_mismatch: the chunk is dropped, the ack
        # never advances past it, the stream carries on.
        reject("invalid_payload", str(exc), header.sequence)
        return
    except DuplicateSequenceError:
        tracker.note_duplicate(header.sequence)
        return
    except (SessionEndedError, NotFoundError):
        raise
    except Exception:
        logger.exception(
            "storing chunk %d of session %s failed", header.sequence, session.id
        )
        reject("storage_failure", "the chunk could not be stored; retransmit it", header.sequence)
        return
    tracker.note_stored(segment.sequence, segment.byte_size)
