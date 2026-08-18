"""The wakeword gate: triggering, pre-roll, window close, re-arm, drops."""

import asyncio
from uuid import uuid4

from live.base import LivePipeline
from live.config import LiveSettings
from live.detect import StubWakewordDetector
from live.gate import WakewordGate

# 16 kHz mono s16le: 32 bytes per ms. Frames of 64 bytes are 2 ms each.
FRAME = b"\x00" * 64
WAKE = b"wake" + b"\x00" * 60


def settings(**overrides) -> LiveSettings:
    values = {
        "wakeword": "wake",
        "preroll_ms": 4,  # two 2ms frames
        "gate_window_ms": 10,  # five 2ms frames
        "pipeline_queue_frames": 64,
    }
    values.update(overrides)
    return LiveSettings(_env_file=None, **values)


class RecordingPipeline(LivePipeline):
    name = "rec"
    instances: list["RecordingPipeline"] = []

    def __init__(self) -> None:
        self.frames = []
        self.done = False
        RecordingPipeline.instances.append(self)

    async def run(self, ctx, frames) -> None:
        ctx.emit("started", {})
        async for frame in frames:
            self.frames.append(frame)
        self.done = True
        ctx.emit("finished", {"frames": len(self.frames)})


def gate(**overrides) -> tuple[WakewordGate, list]:
    RecordingPipeline.instances = []
    events = []
    built = WakewordGate(
        StubWakewordDetector("wake"),
        settings(**overrides),
        (RecordingPipeline,),
        session_id=uuid4(),
        user_id=uuid4(),
        sample_rate_hz=16000,
        channels=1,
        emit=lambda effect, event, data: events.append((effect, event, data)),
    )
    return built, events


async def _feed(target: WakewordGate, frames: list[bytes], start: int = 0) -> None:
    from live.base import LiveFrame

    for offset, pcm in enumerate(frames):
        await target.feed(LiveFrame(sequence=start + offset, pcm=pcm))


async def test_no_trigger_without_wakeword() -> None:
    target, events = gate()
    await _feed(target, [FRAME] * 10)
    assert not target.active
    assert not RecordingPipeline.instances
    assert not events


async def test_trigger_delivers_preroll_then_window() -> None:
    target, events = gate()
    await _feed(target, [FRAME, FRAME, FRAME, WAKE])  # ring keeps the last 2
    assert target.active
    [pipeline] = RecordingPipeline.instances

    await _feed(target, [FRAME] * 5, start=4)  # fills the 10ms window
    assert not target.active
    assert pipeline.done
    # Pre-roll (2 frames incl. the wake frame) plus the 5 window frames.
    assert [frame.sequence for frame in pipeline.frames] == [2, 3, 4, 5, 6, 7, 8]
    assert [event for _, event, _ in events] == ["started", "finished"]
    assert events[0][0] == "rec"


async def test_wakeword_split_across_frames_triggers() -> None:
    target, _ = gate()
    await _feed(target, [b"\x00" * 62 + b"wa", b"ke" + b"\x00" * 62])
    assert target.active


async def test_rearms_after_window() -> None:
    target, events = gate()
    await _feed(target, [WAKE])
    await _feed(target, [FRAME] * 5, start=1)
    assert not target.active
    await _feed(target, [WAKE], start=6)
    assert target.active
    assert len(RecordingPipeline.instances) == 2
    await target.close()
    assert [event for _, event, _ in events].count("finished") == 2


async def test_close_finishes_open_window() -> None:
    target, events = gate()
    await _feed(target, [WAKE, FRAME])
    assert target.active
    await target.close()
    assert not target.active
    assert RecordingPipeline.instances[0].done
    assert ("rec", "finished", {"frames": 2}) in events


async def test_slow_pipeline_drops_frames_without_stalling() -> None:
    class Stuck(LivePipeline):
        name = "stuck"
        instance = None

        def __init__(self) -> None:
            self.frames = []
            self.release = asyncio.Event()
            Stuck.instance = self

        async def run(self, ctx, frames) -> None:
            await self.release.wait()
            async for frame in frames:
                self.frames.append(frame)

    events = []
    target = WakewordGate(
        StubWakewordDetector("wake"),
        settings(pipeline_queue_frames=4, gate_window_ms=1000),
        (Stuck,),
        session_id=uuid4(),
        user_id=uuid4(),
        sample_rate_hz=16000,
        channels=1,
        emit=lambda *args: events.append(args),
    )
    await _feed(target, [WAKE])
    await asyncio.wait_for(_feed(target, [FRAME] * 50, start=1), timeout=1.0)
    Stuck.instance.release.set()
    await target.close()
    assert len(Stuck.instance.frames) <= 4  # everything older was dropped
