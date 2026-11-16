"""Stand-in model services for the e2e suite.

Speaks just enough of the transcription, diarization and classification
contracts for the worker tiers, with deterministic canned answers; response
bodies echo the received byte count so tests can assert the clip actually
crossed the wire. Stdlib only — runs as ``python -m
tests.e2e.stub_audio_services PORT`` next to the real worker process.
"""

import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

STUB_TEXT = "hello from the stub transcriber"
# Clip-relative ms, like a real timestamped backend; the tier offsets them
# onto the session timeline.
STUB_WORDS = [
    {"word": word, "start_ms": i * 20, "end_ms": i * 20 + 15}
    for i, word in enumerate(STUB_TEXT.split())
]
STUB_LANGUAGE = "en"
# The second A/B model, selected with ?model=whisper — distinct text so
# candidates are tellable-apart in assertions.
STUB_WHISPER_TEXT = "greetings from the stub whisper"
STUB_WHISPER_WORDS = [
    {"word": word, "start_ms": i * 20, "end_ms": i * 20 + 15}
    for i, word in enumerate(STUB_WHISPER_TEXT.split())
]

# Two turns and two speakers, fixed: the e2e sessions are ~300ms of tone.
# No per-turn fields — this is deliberately the OLD service shape, so every
# test that goes through it locks in backward compatibility.
STUB_TURNS = [
    {"start_ms": 0, "end_ms": 150, "speaker": "SPEAKER_00"},
    {"start_ms": 150, "end_ms": 300, "speaker": "SPEAKER_01"},
]
STUB_EMBEDDINGS = {
    "SPEAKER_00": [1.0, 0.0, 0.0, 0.0],
    "SPEAKER_01": [0.0, 1.0, 0.0, 0.0],
}
STUB_DIAR_MODEL = "stub-diarizer"

# Uploads over this many bytes get the split scenario instead: SPEAKER_00
# carries two orthogonal per-turn voice groups (a label pyannote should have
# split), SPEAKER_01 stays an old-style turn without per-turn fields. The
# regular ~300ms e2e sessions post ~10KB; a test opts in by uploading ≥1s of
# audio (~32KB). Keep any diarize-asserting session below ~600ms unless it
# wants this response.
STUB_SPLIT_THRESHOLD_BYTES = 20_000
STUB_SPLIT_TURNS = [
    {"start_ms": 0, "end_ms": 250, "speaker": "SPEAKER_00",
     "overlap_ms": 50, "clean_ms": 200, "embedding": [1.0, 0.0, 0.0, 0.0]},
    {"start_ms": 250, "end_ms": 500, "speaker": "SPEAKER_00",
     "overlap_ms": 0, "clean_ms": 250, "embedding": [1.0, 0.0, 0.0, 0.0]},
    {"start_ms": 500, "end_ms": 700, "speaker": "SPEAKER_00",
     "overlap_ms": 0, "clean_ms": 200, "embedding": [0.0, 1.0, 0.0, 0.0]},
    {"start_ms": 750, "end_ms": 950, "speaker": "SPEAKER_00",
     "overlap_ms": 0, "clean_ms": 200, "embedding": [0.0, 1.0, 0.0, 0.0]},
    {"start_ms": 0, "end_ms": 150, "speaker": "SPEAKER_01"},
]
STUB_SPLIT_EMBEDDINGS = {
    "SPEAKER_00": [0.5, 0.5, 0.0, 0.0],  # the blended aggregate: never stored
    "SPEAKER_01": [0.0, 0.0, 1.0, 0.0],  # fallback for the old-style label
}

# Two windows, fixed: Music persists across both (qualifies as a session tag
# with the default min_consecutive=2), Speech appears once (window-only).
STUB_WINDOWS = [
    {
        "start_ms": 0,
        "end_ms": 150,
        "labels": [
            {"label": "Music", "score": 0.9},
            {"label": "Speech", "score": 0.35},
        ],
        "embedding": [1.0, 0.0, 0.0, 0.0],
    },
    {
        "start_ms": 150,
        "end_ms": 300,
        "labels": [{"label": "Music", "score": 0.8}],
        "embedding": [0.0, 1.0, 0.0, 0.0],
    },
]
STUB_TAGGER_MODEL = "stub-tagger"
STUB_CLAP_MODEL = "stub-clap"
# Nearest STUB_WINDOWS[0] by cosine distance, so search tests rank window one first.
STUB_TEXT_EMBEDDING = [1.0, 0.0, 0.0, 0.0]
# The vision sidecar, under {base}/vision: one identical vector per image, so a
# short clip yields exactly its "first" keyframe and every frame matches the
# text query at distance 0.
STUB_VISION_MODEL = "stub-siglip"
STUB_VISION_EMBEDDING = [0.0, 0.0, 0.0, 1.0]
# The chat endpoint, under {base}/llm: describes a moment by echoing what it
# was sent, and links every candidate pair as "depicts".
STUB_LLM_MODEL = "stub-llm"


def _moment_reply(request: dict) -> dict:
    parts = request["messages"][1]["content"]
    text = parts[0]["text"]
    images = sum(1 for p in parts if p.get("type") == "image_url")
    labels = re.findall(r"^([UKPS]\d+)  ", text, re.MULTILINE)
    pairs = re.findall(r"^([KP]\d+) ~ ([US]\d+)$", text, re.MULTILINE)
    content = {
        "title": f"stub moment with {images} images",
        "summary": f"Items: {' '.join(labels)}.",
        "people": ["voice 1"],
        "topics": ["test"],
        "visible_text": [],
        "links": [
            {"from": a, "to": b, "relation": "depicts", "confidence": 0.9, "why": "overlap"}
            for a, b in pairs
        ]
        + [{"from": "X1", "to": "U1", "relation": "depicts"}],  # must be dropped
    }
    return {
        "model": STUB_LLM_MODEL,
        "choices": [{"message": {"role": "assistant", "content": json.dumps(content)}}],
        "usage": {"prompt_tokens": len(text), "completion_tokens": 10, "total_tokens": len(text) + 10},
    }


def _automation_reply(request: dict) -> dict:
    """Applies when the moment mentions a deadline; fills each field with a stub value."""
    text = request["messages"][1]["content"][0]["text"]
    fields = re.findall(r'^- "([^"]+)"', text, re.MULTILINE)
    content = {
        "applies": "deadline" in text.split("MOMENT", 1)[1].lower(),
        "reason": "stub verdict",
        "fields": {name: f"stub {name}" for name in fields},
    }
    return {
        "model": STUB_LLM_MODEL,
        "choices": [{"message": {"role": "assistant", "content": json.dumps(content)}}],
        "usage": {"total_tokens": 1},
    }


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 (http.server's naming)
        length = int(self.headers.get("content-length", 0))
        body = self.rfile.read(length)
        url = urlparse(self.path)
        if url.path.endswith("/llm/chat/completions"):
            request = json.loads(body)
            automation = request["messages"][0]["content"].startswith("You check one moment")
            payload = (_automation_reply if automation else _moment_reply)(request)
        elif "/vision/" in url.path:
            if url.path.endswith("/images/embeddings"):
                count = body.count(b'name="files"')
                vectors = [STUB_VISION_EMBEDDING] * count
            elif url.path.endswith("/text/embeddings"):
                vectors = [STUB_VISION_EMBEDDING] * len(json.loads(body)["texts"])
            else:
                self.send_error(404)
                return
            payload = {"embeddings": vectors, "model": STUB_VISION_MODEL}
        elif url.path.endswith("/audio/transcriptions"):
            model = parse_qs(url.query).get("model", ["parakeet"])[0]
            whisper = model == "whisper"
            payload = {
                "text": STUB_WHISPER_TEXT if whisper else STUB_TEXT,
                "words": STUB_WHISPER_WORDS if whisper else STUB_WORDS,
                "language": STUB_LANGUAGE,
                "received_bytes": len(body),
            }
        elif self.path.endswith("/audio/diarizations"):
            split = len(body) > STUB_SPLIT_THRESHOLD_BYTES
            payload = {
                "turns": STUB_SPLIT_TURNS if split else STUB_TURNS,
                "embeddings": STUB_SPLIT_EMBEDDINGS if split else STUB_EMBEDDINGS,
                "model": STUB_DIAR_MODEL,
                "received_bytes": len(body),
            }
        elif self.path.endswith("/audio/analyze"):
            payload = {
                "windows": STUB_WINDOWS,
                "window_ms": 10_000,
                "hop_ms": 5_000,
                "models": {"tagger": STUB_TAGGER_MODEL, "clap": STUB_CLAP_MODEL},
                "received_bytes": len(body),
            }
        elif self.path.endswith("/text/embeddings"):
            payload = {"embeddings": [STUB_TEXT_EMBEDDING], "model": STUB_CLAP_MODEL}
        else:
            self.send_error(404)
            return
        encoded = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("content-length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args: object) -> None:
        pass  # keep the test output quiet


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), _Handler).serve_forever()
