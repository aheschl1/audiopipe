"""The tap degrades to a no-op when no worker is listening."""

import asyncio
from uuid import uuid4

from live.config import LiveSettings
from live.tap import LiveTap


async def test_send_frame_never_blocks_or_raises_without_worker(tmp_path) -> None:
    settings = LiveSettings(
        _env_file=None, tap_queue_frames=8, reconnect_backoff_seconds=0.05
    )
    tap = LiveTap(settings, str(tmp_path / "absent.sock"))
    handle = tap.attach(
        session_id=uuid4(),
        user_id=uuid4(),
        sample_rate_hz=16000,
        channels=1,
        effects=(),
        on_event=lambda event: None,
    )
    for sequence in range(1000):  # far past the queue bound
        handle.send_frame(sequence, b"\x00" * 64)
    await asyncio.sleep(0.2)  # a few failed connect attempts happen meanwhile
    handle.send_frame(1000, b"\x00" * 64)
    handle.detach()
    handle.send_frame(1001, b"\x00" * 64)  # after detach: still a silent no-op
