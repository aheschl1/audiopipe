"""Worker-side state for one tap connection (one streaming websocket)."""

import asyncio
import logging

from live.config import LiveSettings
from live.protocol import SessionHello

logger = logging.getLogger(__name__)


class SessionStream:
    """One attached stream. Consumes frames; the wakeword gate lands next."""

    def __init__(
        self, hello: SessionHello, settings: LiveSettings, writer: asyncio.StreamWriter
    ) -> None:
        self._hello = hello
        self._frames = 0

    async def feed(self, sequence: int, pcm: bytes) -> None:
        self._frames += 1

    async def close(self) -> None:
        logger.info(
            "detached session %s after %d frames", self._hello.session_id, self._frames
        )
