"""Image and video transforms for the visual pipelines, on PyAV.

Synchronous and CPU-bound — callers run them in a thread. Rotation metadata
(phone display matrices) is applied to the pixels, so every output is upright
without relying on the player to honour it.
"""

import io
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import av
import numpy as np
from av.video.frame import VideoFrame

THUMB_SIDE = 320
PROXY_SIDE = 720
KEYFRAME_SIDE = 1024
CANDIDATE_SIDE = 384
FRAMES_PER_SECOND = 1.0
MAX_FRAMES = 24


@dataclass(frozen=True)
class Still:
    data: bytes
    width: int
    height: int
    offset_ms: int = 0


@dataclass(frozen=True)
class Candidate:
    offset_ms: int
    jpeg: bytes
    sharpness: float
    brightness: float


@dataclass(frozen=True)
class Proxy:
    data: bytes
    width: int
    height: int
    duration_ms: int


def frame_count(duration_ms: int) -> int:
    return max(1, min(MAX_FRAMES, round(duration_ms / 1000 * FRAMES_PER_SECOND)))


def _even(n: float) -> int:
    return max(2, int(round(n / 2)) * 2)


def _upright(frame: VideoFrame, max_side: int) -> np.ndarray:
    """RGB pixels scaled to fit ``max_side`` and rotated per the display matrix."""
    scale = min(1.0, max_side / max(frame.width, frame.height))
    arr = frame.to_ndarray(
        format="rgb24", width=_even(frame.width * scale), height=_even(frame.height * scale)
    )
    turns = round(frame.rotation / 90) % 4
    return np.ascontiguousarray(np.rot90(arr, turns)) if turns else arr


def _jpeg(arr: np.ndarray) -> Still:
    height, width = arr.shape[:2]
    codec = av.CodecContext.create("mjpeg", "w")
    codec.width, codec.height = width, height
    codec.pix_fmt = "yuvj420p"
    codec.time_base = 1  # type: ignore[assignment]
    codec.options = {"qmin": "2", "qmax": "5"}
    frame = VideoFrame.from_ndarray(arr, format="rgb24").reformat(format="yuvj420p")
    data = b"".join(bytes(p) for p in [*codec.encode(frame), *codec.encode(None)])
    return Still(data=data, width=width, height=height)


def image_thumbnail(data: bytes, max_side: int = THUMB_SIDE) -> Still:
    with av.open(io.BytesIO(data)) as container:
        frame = next(container.decode(video=0))
        return _jpeg(_upright(frame, max_side))


def _duration_ms(container: av.container.InputContainer) -> int:
    stream = container.streams.video[0]
    if stream.duration is not None and stream.time_base is not None:
        return int(stream.duration * stream.time_base * 1000)
    return int((container.duration or 0) / 1000)


def video_frames(
    data: bytes, count: Callable[[int], int] = frame_count, max_side: int = THUMB_SIDE
) -> list[Still]:
    """Evenly spaced stills across the clip, each at its offset from the first frame."""
    with av.open(io.BytesIO(data)) as container:
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        duration = _duration_ms(container)
        n = count(duration)
        targets = [(i + 0.5) / n * duration for i in range(n)]
        stills: list[Still] = []
        start: float | None = None
        for frame in container.decode(stream):
            if frame.time is None:
                continue
            start = frame.time if start is None else start
            at_ms = (frame.time - start) * 1000
            if len(stills) < n and at_ms >= targets[len(stills)]:
                still = _jpeg(_upright(frame, max_side))
                stills.append(Still(still.data, still.width, still.height, round(at_ms)))
            if len(stills) == n:
                break
        return stills


def sharpness(gray: np.ndarray) -> float:
    """Variance of the 4-neighbour Laplacian: low means blurred or featureless."""
    g = gray.astype(np.float32)
    lap = g[1:-1, :-2] + g[1:-1, 2:] + g[:-2, 1:-1] + g[2:, 1:-1] - 4 * g[1:-1, 1:-1]
    return float(lap.var())


def _gray(arr: np.ndarray) -> np.ndarray:
    return arr @ np.array([0.299, 0.587, 0.114], dtype=np.float32)


def _sampled(data: bytes, wanted: Callable[[float], bool]):
    """Decoded frames whose offset from the first frame ``wanted`` accepts, in order."""
    with av.open(io.BytesIO(data)) as container:
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        start: float | None = None
        for frame in container.decode(stream):
            if frame.time is None:
                continue
            start = frame.time if start is None else start
            at_ms = (frame.time - start) * 1000
            if wanted(at_ms):
                yield round(at_ms), frame


def candidate_frames(data: bytes, fps: float, max_side: int = CANDIDATE_SIDE) -> list[Candidate]:
    """Small upright stills every 1/fps seconds, scored for focus and exposure."""
    step = 1000 / fps
    next_at = 0.0
    out: list[Candidate] = []

    def wanted(at_ms: float) -> bool:
        nonlocal next_at
        if at_ms < next_at:
            return False
        next_at = (at_ms // step + 1) * step
        return True

    for at_ms, frame in _sampled(data, wanted):
        arr = _upright(frame, max_side)
        gray = _gray(arr)
        out.append(Candidate(at_ms, _jpeg(arr).data, sharpness(gray), float(gray.mean())))
    return out


def frames_at(data: bytes, offsets: list[int], max_side: int = KEYFRAME_SIDE) -> dict[int, Still]:
    """Upright stills at these decoded-frame offsets (as candidate_frames reported them)."""
    pending = sorted(set(offsets))
    out: dict[int, Still] = {}

    def wanted(at_ms: float) -> bool:
        return bool(pending) and round(at_ms) >= pending[0]

    for at_ms, frame in _sampled(data, wanted):
        still = _jpeg(_upright(frame, max_side))
        out[pending.pop(0)] = Still(still.data, still.width, still.height, at_ms)
        if not pending:
            break
    return out


def image_still(data: bytes, max_side: int = KEYFRAME_SIDE) -> tuple[Still, Candidate]:
    """A photo as both a keyframe-sized still and a scored candidate."""
    with av.open(io.BytesIO(data)) as container:
        frame = next(container.decode(video=0))
        big, small = _upright(frame, max_side), _upright(frame, CANDIDATE_SIDE)
    gray = _gray(small)
    return _jpeg(big), Candidate(0, _jpeg(small).data, sharpness(gray), float(gray.mean()))


def _upright_size(data: bytes, max_side: int) -> tuple[int, int]:
    with av.open(io.BytesIO(data)) as probe:
        for frame in probe.decode(video=0):
            arr = _upright(frame, max_side)
            return arr.shape[1], arr.shape[0]
    raise ValueError("clip has no decodable video frames")


def proxy(data: bytes, max_side: int = PROXY_SIDE) -> Proxy:
    """An upright H.264/AAC MP4 (moov first, so browsers seek it) of any decodable clip."""
    width, height = _upright_size(data, max_side)
    with tempfile.TemporaryDirectory() as tmp, av.open(io.BytesIO(data)) as src:
        path = Path(tmp) / "proxy.mp4"
        vin = src.streams.video[0]
        vin.thread_type = "AUTO"
        ain = src.streams.audio[0] if src.streams.audio else None
        with av.open(str(path), "w", format="mp4", options={"movflags": "+faststart"}) as out:
            vout = out.add_stream(
                "libx264", rate=vin.average_rate or 30, options={"crf": "26", "preset": "veryfast"}
            )
            vout.pix_fmt = "yuv420p"
            vout.width, vout.height = width, height
            vout.codec_context.time_base = vin.time_base
            vout.time_base = vin.time_base
            copy_audio = ain is not None and ain.codec_context.name == "aac"
            aout = (
                out.add_stream_from_template(ain)
                if copy_audio
                else out.add_stream("aac", rate=ain.rate)
                if ain is not None
                else None
            )
            last_ms = 0.0
            first: float | None = None
            streams = [vin] if ain is None else [vin, ain]
            for packet in src.demux(*streams):
                if packet.stream is ain:
                    if aout is None or packet.dts is None:
                        continue
                    if copy_audio:
                        packet.stream = aout
                        out.mux(packet)
                    else:
                        for audio in packet.decode():
                            out.mux(aout.encode(audio))
                    continue
                for frame in packet.decode():
                    upright = VideoFrame.from_ndarray(
                        _upright(frame, max_side), format="rgb24"
                    ).reformat(
                        format="yuv420p"
                    )
                    upright.pts, upright.time_base = frame.pts, frame.time_base
                    out.mux(vout.encode(upright))
                    if frame.time is not None:
                        first = frame.time if first is None else first
                        last_ms = (frame.time - first) * 1000
            out.mux(vout.encode(None))
            if aout is not None and not copy_audio:
                out.mux(aout.encode(None))
        return Proxy(path.read_bytes(), width, height, round(last_ms))
