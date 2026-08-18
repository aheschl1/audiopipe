"""Stub live pipelines."""

import logging
from collections.abc import AsyncIterator

from live.base import LiveFrame, LivePipeline, LiveSessionContext

logger = logging.getLogger(__name__)


class CountingPipeline(LivePipeline):
    """Counts a gated window's frames — exercises the whole live loop."""

    name = "live-counter"

    async def run(self, ctx: LiveSessionContext, frames: AsyncIterator[LiveFrame]) -> None:
        ctx.emit("started", {})
        count = 0
        byte_size = 0
        try:
            async for frame in frames:
                count += 1
                byte_size += len(frame.pcm)
        finally:
            logger.info(
                "live-counter for session %s: %d frames, %d bytes",
                ctx.session_id, count, byte_size,
            )
            ctx.emit("finished", {"frames": count, "bytes": byte_size})
