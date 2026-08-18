"""The live pipeline contract: a consumer of one gated audio stream.

A pipeline knows nothing about wakewords. The gate opens a window, hands the
pipeline an iterator of frames (pre-roll first), and closes the iterator when
the window ends; ``run`` returning is the pipeline's finalization.
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, ClassVar
from uuid import UUID


@dataclass(frozen=True, slots=True)
class LiveFrame:
    sequence: int
    pcm: bytes


@dataclass(frozen=True, slots=True)
class LiveSessionContext:
    """What a pipeline may know about the stream it consumes.

    ``emit(event, data)`` publishes an effect event back to the session's
    websocket clients; it never blocks.
    """

    session_id: UUID
    user_id: UUID
    sample_rate_hz: int
    channels: int
    emit: Callable[[str, dict[str, Any]], None]


class LivePipeline(ABC):
    """One live consumer; ``name`` is the effect name clients request."""

    name: ClassVar[str]

    @abstractmethod
    async def run(self, ctx: LiveSessionContext, frames: AsyncIterator[LiveFrame]) -> None:
        """Consume one gated window of audio. May await freely; the gate's
        queue drops behind a slow consumer rather than stalling the stream."""
