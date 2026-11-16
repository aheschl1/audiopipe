"""Seed the public demo account from local clips, through the real ingest API.

Each scene is one recording session: its video goes up as a `video` segment,
its audio track is streamed over the v2 websocket as live PCM, and a fixed
GPS point stands in for the glasses' location.
"""

import asyncio
import json
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import click
import httpx
import websockets

from api.schema.stream import ChunkHeader, FRAME_ENVELOPE, encode_audio_frame, encode_chunk
from database.pipe import DatabasePipe
from database.schema.automations import AutomationWrite

SAMPLE_RATE = 16_000
FRAME_BYTES = 640  # 20 ms of 16 kHz mono s16le
DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "demo"


@dataclass(frozen=True)
class Scene:
    file: str
    label: str
    offset: timedelta  # from the first scene's start


SCENES = (
    Scene("clip1demo.mp4", "Drop-off, lane 1", timedelta(0)),
    Scene("clip2demo.mp4", "Inspection, lane 1", timedelta(minutes=9)),
)

# The account is read-only, so its example automation is seeded here.
AUTOMATION = AutomationWrite.model_validate({
    "name": "Work order",
    "conditions": {
        "ask": "Is this a vehicle service visit, such as a drop-off, an inspection or a repair quote?",
    },
    "actions": [{"type": "extract", "fields": [
        {"name": "Vehicle", "description": "The vehicle's make, model or description"},
        {"name": "Complaint", "description": "What the customer said was wrong, close to their words"},
        {"name": "Finding", "description": "What the inspection found, and where on the vehicle"},
        {"name": "Quote", "description": "The price or estimate given for the repair"},
        {"name": "Next step", "description": "What happens next, e.g. approval or pickup"},
    ]}],
})

# A made-up point, not the location of any real shop.
SHOP = {"lat": 51.0262, "lon": -114.0396, "accuracy_m": 6.0}


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def _duration_ms(path: Path) -> int:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return round(float(out.stdout) * 1000)


def prepare(clip: Path, work: Path) -> tuple[Path, bytes, int]:
    """Split a clip into a silent video file and raw 16 kHz mono PCM."""
    video = work / f"{clip.stem}.video.mp4"
    pcm = work / f"{clip.stem}.pcm"
    _ffmpeg("-i", str(clip), "-an", "-c:v", "copy", "-movflags", "+faststart", str(video))
    _ffmpeg("-i", str(clip), "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", str(pcm))
    return video, pcm.read_bytes(), _duration_ms(video)


async def _stream(ws_url: str, token: str, pcm: bytes, start: datetime, duration_ms: int) -> int:
    """Stream one location batch and the PCM, finish, and return the acked sequence."""
    hello = {
        "type": "hello",
        "version": 2,
        "defaults": {"codec": "pcm_s16le", "sample_rate_hz": SAMPLE_RATE, "channels": 1},
    }
    async with websockets.connect(
        ws_url, additional_headers={"Authorization": f"Bearer {token}"}, max_size=None
    ) as ws:
        await ws.send(json.dumps(hello))
        welcome = json.loads(await ws.recv())
        if welcome.get("type") != "welcome":
            raise click.ClickException(f"stream refused: {welcome}")
        sequence = welcome["next_sequence"]

        t0 = int(start.timestamp() * 1000)
        points = [{**SHOP, "t": t0}, {**SHOP, "t": t0 + duration_ms}]
        header = ChunkHeader(
            sequence=sequence,
            resource="location",
            content_type="application/json",
            captured_at=start,
        )
        await ws.send(bytes([FRAME_ENVELOPE]) + encode_chunk(header, json.dumps({"points": points}).encode()))
        sequence += 1

        for offset in range(0, len(pcm) - len(pcm) % 2, FRAME_BYTES):
            chunk = pcm[offset : offset + FRAME_BYTES]
            at = start + timedelta(milliseconds=offset / 2 / SAMPLE_RATE * 1000)
            await ws.send(encode_audio_frame(sequence, chunk, at))
            sequence += 1

        ended_at = start + timedelta(milliseconds=duration_ms)
        await ws.send(json.dumps({"type": "finish", "ended_at": ended_at.isoformat()}))
        through = -1
        async for message in ws:
            if isinstance(message, bytes):
                continue
            event = json.loads(message)
            if event["type"] == "ack":
                through = event["through"]
            elif event["type"] == "error":
                raise click.ClickException(f"stream error: {event}")
            elif event["type"] == "bye":
                break
        if through != sequence - 1:
            raise click.ClickException(f"stream acked through {through}, sent {sequence - 1}")
        return through


async def seed_scenes(api: str, token: str, data_dir: Path, first_start: datetime) -> list[str]:
    """Ingest every scene as its own session; returns the new session ids."""
    ws_base = api.replace("https://", "wss://").replace("http://", "ws://")
    headers = {"Authorization": f"Bearer {token}"}
    created: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        async with httpx.AsyncClient(base_url=api, headers=headers, timeout=60) as client:
            for scene in SCENES:
                clip = data_dir / scene.file
                if not clip.exists():
                    raise click.ClickException(f"missing clip {clip}")
                video, pcm, duration_ms = await asyncio.to_thread(prepare, clip, Path(tmp))
                start = first_start + scene.offset

                res = await client.post(
                    "/v1/sessions",
                    json={"device": "demo", "label": scene.label, "started_at": start.isoformat()},
                )
                res.raise_for_status()
                session_id = res.json()["id"]

                res = await client.post(
                    f"/v1/sessions/{session_id}/segments",
                    files={"file": (video.name, video.read_bytes(), "video/mp4")},
                    data={
                        "resource": "video",
                        "captured_at": start.isoformat(),
                        "duration_ms": str(duration_ms),
                    },
                )
                res.raise_for_status()

                through = await _stream(
                    f"{ws_base}/v1/sessions/{session_id}/stream", token, pcm, start, duration_ms
                )
                click.echo(
                    f"  {scene.label}: session {session_id}, "
                    f"{duration_ms / 1000:.1f}s video + audio, acked through {through}"
                )
                created.append(session_id)
    return created


async def add_automation(user_id: UUID, since: datetime) -> bool:
    """Give the demo user the example automation, covering moments made since ``since``.

    False when the user already has one by that name.
    """
    async with DatabasePipe() as pipe:
        if any(a.name == AUTOMATION.name for a in await pipe.automations.list_for_user(user_id)):
            return False
        await pipe.automations.create(user_id, AUTOMATION, active_since=since)
    return True


def default_start() -> datetime:
    """Recent enough to top the moments feed, with room for the later scenes."""
    last = max(scene.offset for scene in SCENES)
    return (datetime.now(UTC) - last - timedelta(minutes=5)).replace(microsecond=0)
