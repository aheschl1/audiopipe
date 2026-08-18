"""The API-side tap: forwards a websocket's audio frames to the live worker.

Strictly best-effort. ``send_frame`` never blocks and never raises; while the
worker is down or slow, frames queue up to a bound and then the oldest drop.
The durable path (the pooler) is elsewhere and never waits on any of this.
"""

import asyncio
import logging
from collections.abc import Callable
from uuid import UUID

from live.config import LiveSettings
from live.protocol import (
    MSG_EVENT,
    MSG_HELLO,
    PROTOCOL_VERSION,
    Event,
    SessionHello,
    decode_json,
    encode_audio,
    encode_message,
    read_message,
)

logger = logging.getLogger(__name__)


class LiveTap:
    """Factory for per-connection handles; holds only settings and the path."""

    def __init__(self, settings: LiveSettings, socket_path: str) -> None:
        self._settings = settings
        self._socket_path = socket_path

    def attach(
        self,
        *,
        session_id: UUID,
        user_id: UUID,
        sample_rate_hz: int,
        channels: int,
        effects: tuple[str, ...],
        on_event: Callable[[Event], None],
    ) -> "LiveSessionHandle":
        hello = SessionHello(
            version=PROTOCOL_VERSION,
            session_id=session_id,
            user_id=user_id,
            sample_rate_hz=sample_rate_hz,
            channels=channels,
            effects=effects,
        )
        return LiveSessionHandle(self._settings, self._socket_path, hello, on_event)


class LiveSessionHandle:
    """One UDS connection to the worker, alive for one websocket connection."""

    def __init__(
        self,
        settings: LiveSettings,
        socket_path: str,
        hello: SessionHello,
        on_event: Callable[[Event], None],
    ) -> None:
        self._settings = settings
        self._socket_path = socket_path
        self._hello = hello
        self._on_event = on_event
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(settings.tap_queue_frames)
        self._closed = False
        self._task = asyncio.create_task(self._run())

    def send_frame(self, sequence: int, pcm: bytes) -> None:
        if self._closed:
            return
        message = encode_audio(sequence, pcm)
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            self._queue.get_nowait()  # drop the oldest; live audio must stay fresh
            self._queue.put_nowait(message)

    def detach(self) -> None:
        self._closed = True
        self._task.cancel()

    async def _run(self) -> None:
        backoff = self._settings.reconnect_backoff_seconds
        while not self._closed:
            try:
                reader, writer = await asyncio.open_unix_connection(self._socket_path)
            except OSError:
                await asyncio.sleep(backoff)
                backoff = min(self._settings.reconnect_backoff_cap_seconds, backoff * 2)
                continue
            backoff = self._settings.reconnect_backoff_seconds
            events = asyncio.create_task(self._read_events(reader))
            try:
                writer.write(
                    encode_message(MSG_HELLO, self._hello.model_dump_json().encode())
                )
                while True:
                    writer.write(await self._queue.get())
                    await writer.drain()
            except (OSError, ConnectionResetError):
                pass  # worker went away; reconnect with a fresh hello
            finally:
                events.cancel()
                writer.close()

    async def _read_events(self, reader: asyncio.StreamReader) -> None:
        while (message := await read_message(reader)) is not None:
            msg_type, body = message
            if msg_type != MSG_EVENT:
                continue
            try:
                self._on_event(decode_json(Event, body))
            except Exception:
                logger.exception("live event dispatch failed")
