"""Tiny real images and clips, encoded with PyAV, for the visual pipelines."""

import io
import tempfile
from fractions import Fraction
from pathlib import Path

import av
import numpy as np
from av.audio.frame import AudioFrame
from av.video.frame import VideoFrame


def _gradient(width: int, height: int, shade: int) -> np.ndarray:
    arr = np.zeros((height, width, 3), dtype=np.uint8)
    arr[..., 0] = np.linspace(0, 255, width, dtype=np.uint8)[None, :]
    arr[..., 1] = shade
    return arr


def jpeg_bytes(width: int = 640, height: int = 480) -> bytes:
    buf = io.BytesIO()
    with av.open(buf, "w", format="image2pipe") as out:
        stream = out.add_stream("mjpeg", rate=1)
        stream.width, stream.height, stream.pix_fmt = width, height, "yuvj420p"
        frame = VideoFrame.from_ndarray(_gradient(width, height, 80), format="rgb24")
        out.mux(stream.encode(frame.reformat(format="yuvj420p")))
        out.mux(stream.encode(None))
    return buf.getvalue()


def mp4_bytes(seconds: int = 3, width: int = 320, height: int = 240, fps: int = 10) -> bytes:
    """An MPEG-4 Part 2 clip with an AAC track — a codec browsers will not play."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "clip.mp4"
        with av.open(str(path), "w", format="mp4") as out:
            video = out.add_stream("mpeg4", rate=fps)
            video.width, video.height, video.pix_fmt = width, height, "yuv420p"
            audio = out.add_stream("aac", rate=16_000)
            for i in range(seconds * fps):
                frame = VideoFrame.from_ndarray(
                    _gradient(width, height, i * 255 // (seconds * fps)), format="rgb24"
                ).reformat(format="yuv420p")
                frame.pts, frame.time_base = i, Fraction(1, fps)
                out.mux(video.encode(frame))
            samples = np.zeros((1, 16_000 * seconds), dtype=np.float32)
            pcm = AudioFrame.from_ndarray(samples, format="fltp", layout="mono")
            pcm.sample_rate, pcm.pts = 16_000, 0
            out.mux(audio.encode(pcm))
            out.mux(audio.encode(None))
            out.mux(video.encode(None))
        return path.read_bytes()
