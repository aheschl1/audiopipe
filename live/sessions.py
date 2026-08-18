"""Worker-side state for one tap connection (one streaming websocket)."""

import asyncio
import logging
from typing import Any

from live import registry
from live.base import LiveFrame
from live.config import LiveSettings
from live.detect import StubWakewordDetector
from live.gate import WakewordGate
from live.protocol import MSG_EVENT, Event, SessionHello, encode_message

logger = logging.getLogger(__name__)


class SessionStream:
    """One attached stream: its gate and the event write-back path."""

    def __init__(
        self, hello: SessionHello, settings: LiveSettings, writer: asyncio.StreamWriter
    ) -> None:
        self._writer = writer
        pipelines = tuple(
            cls for cls in registry.LIVE_PIPELINES if cls.name in hello.effects
        )
        self._gate = WakewordGate(
            StubWakewordDetector(settings.wakeword),
            settings,
            pipelines,
            session_id=hello.session_id,
            user_id=hello.user_id,
            sample_rate_hz=hello.sample_rate_hz,
            channels=hello.channels,
            emit=self._emit,
        )

    def _emit(self, effect: str, event: str, data: dict[str, Any]) -> None:
        # One write() call per message keeps concurrent emitters from
        # interleaving; the tap always reads, so no drain is needed.
        if self._writer.is_closing():
            return
        body = Event(effect=effect, event=event, data=data).model_dump_json().encode()
        self._writer.write(encode_message(MSG_EVENT, body))

    async def feed(self, sequence: int, pcm: bytes) -> None:
        await self._gate.feed(LiveFrame(sequence=sequence, pcm=pcm))

    async def close(self) -> None:
        await self._gate.close()
