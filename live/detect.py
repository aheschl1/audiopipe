"""The wakeword detection seam in front of the gate."""

from typing import Protocol


class WakewordDetector(Protocol):
    def reset(self) -> None: ...

    def feed(self, pcm: bytes) -> bool:
        """Consume audio; True means the wakeword just completed."""
        ...


class StubWakewordDetector:
    """Triggers when the wakeword's UTF-8 bytes appear in the PCM stream.

    This exists only so tests can fire the gate deterministically (embed the
    bytes in a frame payload) — it is not a placeholder detection algorithm.
    A real detector replaces this class behind the same protocol.
    """

    def __init__(self, wakeword: str) -> None:
        self._needle = wakeword.encode()
        self._tail = b""

    def reset(self) -> None:
        self._tail = b""

    def feed(self, pcm: bytes) -> bool:
        window = self._tail + pcm
        if self._needle in window:
            self._tail = b""
            return True
        self._tail = window[-(len(self._needle) - 1):] if len(self._needle) > 1 else b""
        return False
