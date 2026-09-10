"""Audio tagging + embedding server: CED (AudioSet 527) and LAION-CLAP.

The owned serving layer for the audio-pipeline's audio-tag tier. One container
serves both models because they consume the same audio windows: CED scores 527
sound-event classes per window, CLAP embeds each window into its joint
audio-text space (the retrieval index for text->audio search).

    POST /v1/audio/analyze        multipart WAV ->
        {"windows": [{"start_ms", "end_ms",
                      "labels": [{"label", "score"}, ...],   # top-K sigmoid
                      "embedding": [floats]},                # L2-normalized CLAP
                     ...],
         "window_ms": ..., "hop_ms": ...,
         "models": {"tagger": "...", "clap": "..."}}
    POST /v1/text/embeddings      {"texts": [...]} ->
        {"embeddings": [[floats], ...], "model": "..."}      # CLAP text side
    GET  /v1/models               both loaded model ids
    GET  /health                  503 while the models are still loading;
                                  200 "idle-unloaded" after an idle eviction

Windowing (10s/5s hop by default) happens here, not in the caller: the tier
uploads ~2-minute clips and gets per-window rows back, so a long session is a
handful of requests instead of hundreds. Window times are relative to the
uploaded clip; the caller adds its span offset.

The models load in a background thread so the port binds immediately and the
compose healthcheck gates readiness. Inference is serialized with a lock: one
GPU, one request at a time; throughput comes from batching windows inside a
request.

With IDLE_UNLOAD_SECONDS set, a watchdog frees the CUDA memory after that
long without inference; the next request blocks while the models reload
(callers must budget their timeout for that, not just a contended GPU).
With IDLE_UNLOAD_EXIT the watchdog exits instead — only process death
releases the CUDA context itself — and the restarted process loads lazily,
holding zero GPU memory until the first request.
"""

import asyncio
import gc
import io
import logging
import math
import os
import threading
import time

import numpy as np
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

TAGGER_ID = os.environ.get("TAGGER_MODEL", "mispeech/ced-base")
CLAP_ID = os.environ.get("CLAP_MODEL", "laion/larger_clap_general")
WINDOW_MS = int(os.environ.get("WINDOW_MS", "10000"))
HOP_MS = int(os.environ.get("HOP_MS", "5000"))
BATCH = int(os.environ.get("BATCH", "16"))
TOP_K = int(os.environ.get("TOP_K", "20"))
# Windows shorter than this are dropped (except a sole window): a 1s tail is
# mostly the previous window again and scores unreliably.
MIN_WINDOW_MS = int(os.environ.get("MIN_WINDOW_MS", "2000"))
# Release CUDA memory after this long without inference; 0 keeps the models
# resident forever.
IDLE_UNLOAD_SECONDS = int(os.environ.get("IDLE_UNLOAD_SECONDS", "0"))
# Exit when the idle timer fires instead of unloading in-process: only
# process death releases the CUDA context itself. The restart policy brings
# back a fresh process, which loads lazily on the first request.
IDLE_UNLOAD_EXIT = os.environ.get("IDLE_UNLOAD_EXIT", "0") == "1"
_WATCHDOG_TICK_SECONDS = 30
# A reload that fails leaves the process wedged: the checkpoint is fine, but
# something in this long-lived worker cannot load it again. Retry with backoff,
# then exit so the restart policy hands us a fresh process, which always works.
RELOAD_RETRY_LIMIT = 5
_RELOAD_BACKOFF_SECONDS = 30.0
# Deferred a beat so the request that tripped the limit still gets its 503.
_DIE_DELAY_SECONDS = 2.0

TAGGER_RATE = 16_000  # CED's training rate
CLAP_RATE = 48_000  # CLAP's training rate

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("audio-tagger")

app = FastAPI(title="audio-tagger")

_models = None  # (tagger_fe, tagger, id2label, clap_processor, clap)
_load_error: str | None = None
_gpu_lock = threading.Lock()
# Load/unload transitions. Lock order: _lifecycle may be held while taking
# _gpu_lock, never the reverse (the watchdog only tries _gpu_lock without
# blocking, so there is no cycle).
_lifecycle = threading.Lock()
_last_used = time.monotonic()
_ever_loaded = False  # a cold start that never worked must not exit-loop
_reload_failures = 0
_retry_after = 0.0
_state = "loading"  # "loading" | "loaded" | "idle" | "failed"


class ModelsUnavailable(RuntimeError):
    """The models are missing and could not be (re)loaded."""


def _die(reason: str) -> None:
    """Exit so the container's restart policy gives us a fresh process."""
    logger.error("%s; exiting for a restart", reason)
    threading.Timer(_DIE_DELAY_SECONDS, os._exit, args=(1,)).start()


def _record_load_result() -> None:
    """Backoff and give-up bookkeeping, run after every load attempt."""
    global _ever_loaded, _reload_failures, _retry_after, _last_used
    if _state == "loaded":
        _last_used = time.monotonic()
        _ever_loaded = True
        _reload_failures = 0
        _retry_after = 0.0
        return
    _retry_after = time.monotonic() + _RELOAD_BACKOFF_SECONDS
    if not _ever_loaded:
        return  # never loaded at all: a restart would fail the same way
    _reload_failures += 1
    if _reload_failures >= RELOAD_RETRY_LIMIT:
        _die(f"reload failed {_reload_failures} times running")


def _load_models() -> None:
    global _models, _load_error, _state, _last_used
    try:
        import torch
        from transformers import (
            AutoFeatureExtractor,
            AutoModelForAudioClassification,
            ClapModel,
            ClapProcessor,
        )

        device = "cuda" if torch.cuda.is_available() else "cpu"
        # fp16 halves VRAM and is accurate enough for sigmoid tag scores; CPU
        # fallback stays fp32 (half-precision CPU inference is slow in torch).
        dtype = torch.float16 if device == "cuda" else torch.float32

        logger.info("loading %s ...", TAGGER_ID)
        tagger_fe = AutoFeatureExtractor.from_pretrained(TAGGER_ID, trust_remote_code=True)
        tagger = (
            AutoModelForAudioClassification.from_pretrained(
                TAGGER_ID, trust_remote_code=True, torch_dtype=dtype
            )
            .to(device)
            .eval()
        )
        logger.info("loading %s ...", CLAP_ID)
        clap_processor = ClapProcessor.from_pretrained(CLAP_ID)
        clap = ClapModel.from_pretrained(CLAP_ID, torch_dtype=dtype).to(device).eval()

        _models = (tagger_fe, tagger, dict(tagger.config.id2label), clap_processor, clap)
        _load_error = None
        _state = "loaded"
        _last_used = time.monotonic()
        logger.info("models ready on %s (%s)", device, dtype)
    except Exception as exc:  # surfaced via /health; the container stays up
        _load_error = f"{type(exc).__name__}: {exc}"
        _state = "failed"
        logger.exception("model load failed")
    _record_load_result()


def _ensure_loaded():
    """The model tuple, reloading it first if the watchdog evicted it.

    Returns the tuple rather than having callers read the global: a reference
    taken under ``_lifecycle`` stays alive (and usable) even if an unload
    lands before the caller reaches ``_gpu_lock``.

    A failed reload leaves the state ``failed``, never ``idle``: the idle
    branch of /health is a deliberate 200, and answering green while every
    request 503s is how a wedged reload goes unnoticed for days.
    """
    global _last_used
    with _lifecycle:
        if _state in ("idle", "failed") and time.monotonic() >= _retry_after:
            logger.info("(re)loading models (state=%s) ...", _state)
            _load_models()
        if _state != "loaded":
            raise ModelsUnavailable(_load_error or "models are not loaded yet")
        _last_used = time.monotonic()
        return _models


def _idle_watchdog() -> None:
    while True:
        time.sleep(_WATCHDOG_TICK_SECONDS)
        _watchdog_tick()


def _watchdog_tick() -> None:
    global _models, _state
    with _lifecycle:
        if _state == "failed" and _ever_loaded and time.monotonic() >= _retry_after:
            # A wedged reload must not wait for traffic: the 2026-08-23
            # outage stalled at 4/5 failures when the queue went quiet, so
            # the watchdog walks the failure count to _die by itself.
            logger.info("watchdog retrying failed (re)load ...")
            _load_models()
            return
        if _state != "loaded" or time.monotonic() - _last_used < IDLE_UNLOAD_SECONDS:
            return
        if not _gpu_lock.acquire(blocking=False):
            return  # inference in flight; not idle after all
        if IDLE_UNLOAD_EXIT:
            # A request racing this exit dies with the connection; the
            # worker's ServiceUnavailable refund absorbs it.
            logger.info("idle for %ss; exiting to release the GPU", IDLE_UNLOAD_SECONDS)
            os._exit(0)
        try:
            import torch

            _models = None
            gc.collect()
            torch.cuda.empty_cache()
            _state = "idle"
            logger.info("idle for %ss; models unloaded", IDLE_UNLOAD_SECONDS)
        finally:
            _gpu_lock.release()


def _load_models_locked() -> None:
    # Under _lifecycle: a request arriving mid-startup can now trigger its own
    # reload off the "failed" state, and the two must not load side by side.
    with _lifecycle:
        _load_models()


@app.on_event("startup")
def _startup() -> None:
    global _state
    if IDLE_UNLOAD_EXIT:
        _state = "idle"  # lazy: zero GPU until the first request loads
    else:
        threading.Thread(target=_load_models_locked, daemon=True).start()
    if IDLE_UNLOAD_SECONDS > 0:
        threading.Thread(target=_idle_watchdog, daemon=True).start()


@app.get("/health")
def health() -> JSONResponse:
    # An idle unload is deliberate, so it stays 200: compose gates the worker
    # on this endpoint, and an evicted-but-healthy container must not flap it.
    if _state in ("loaded", "idle"):
        status = "ok" if _state == "loaded" else "idle-unloaded"
        return JSONResponse(
            {
                "status": status,
                "tagger": TAGGER_ID,
                "clap": CLAP_ID,
                "idle_unload_seconds": IDLE_UNLOAD_SECONDS,
            }
        )
    body = {"status": _state}
    if _load_error is not None:
        body["error"] = _load_error
    return JSONResponse(body, status_code=503)


@app.get("/v1/models")
def models() -> dict:
    return {
        "object": "list",
        "data": [{"id": TAGGER_ID, "object": "model"}, {"id": CLAP_ID, "object": "model"}],
    }


def _decode(data: bytes) -> tuple[np.ndarray, int]:
    """Any soundfile-readable audio -> (mono float32, rate)."""
    import soundfile as sf

    audio, rate = sf.read(io.BytesIO(data), dtype="float32", always_2d=True)
    return audio.mean(axis=1), int(rate)


def _resample(audio: np.ndarray, rate: int, target: int) -> np.ndarray:
    if rate == target:
        return audio
    import soxr

    return soxr.resample(audio, rate, target)


def _window_bounds(total_ms: int) -> list[tuple[int, int]]:
    """[start, end) clip-relative window times at HOP_MS steps."""
    if total_ms <= 0:
        return []
    bounds = []
    for start in range(0, total_ms, HOP_MS):
        end = min(start + WINDOW_MS, total_ms)
        if end - start < MIN_WINDOW_MS and bounds:
            break
        bounds.append((start, end))
        if end == total_ms:
            break
    return bounds


def _analyze(data: bytes) -> dict:
    global _last_used
    import torch

    tagger_fe, tagger, id2label, clap_processor, clap = _ensure_loaded()
    audio, rate = _decode(data)
    total_ms = int(len(audio) * 1000 / rate)
    bounds = _window_bounds(total_ms)
    if not bounds:
        return {
            "windows": [],
            "window_ms": WINDOW_MS,
            "hop_ms": HOP_MS,
            "models": {"tagger": TAGGER_ID, "clap": CLAP_ID},
        }

    at_tagger = _resample(audio, rate, TAGGER_RATE)
    at_clap = _resample(audio, rate, CLAP_RATE)
    tagger_clips = [
        at_tagger[start * TAGGER_RATE // 1000 : end * TAGGER_RATE // 1000]
        for start, end in bounds
    ]
    clap_clips = [
        at_clap[start * CLAP_RATE // 1000 : end * CLAP_RATE // 1000] for start, end in bounds
    ]

    device = next(tagger.parameters()).device
    windows = [{"start_ms": start, "end_ms": end} for start, end in bounds]
    with _gpu_lock, torch.inference_mode():
        for offset in range(0, len(bounds), BATCH):
            batch = tagger_clips[offset : offset + BATCH]
            features = tagger_fe(batch, sampling_rate=TAGGER_RATE, return_tensors="pt")
            features = {k: v.to(device=device, dtype=tagger.dtype) for k, v in features.items()}
            scores = torch.sigmoid(tagger(**features).logits.float())
            top = scores.topk(min(TOP_K, scores.shape[1]), dim=1)
            for row, values, indices in zip(
                windows[offset : offset + BATCH], top.values, top.indices
            ):
                row["labels"] = [
                    {"label": id2label[index], "score": round(value, 4)}
                    for value, index in zip(values.tolist(), indices.tolist())
                ]

        for offset in range(0, len(bounds), BATCH):
            batch = clap_clips[offset : offset + BATCH]
            inputs = clap_processor(
                audio=batch, sampling_rate=CLAP_RATE, return_tensors="pt", padding=True
            )
            inputs = {
                k: v.to(device=device, dtype=clap.dtype if v.is_floating_point() else None)
                for k, v in inputs.items()
            }
            out = clap.get_audio_features(**inputs)
            # transformers >=5 returns the full output object; the projected,
            # normalized embedding sits in pooler_output. <=4 returns a tensor.
            embeddings = (out if torch.is_tensor(out) else out.pooler_output).float()
            embeddings = torch.nn.functional.normalize(embeddings, dim=1)
            for row, vector in zip(windows[offset : offset + BATCH], embeddings):
                row["embedding"] = [round(v, 6) for v in vector.tolist()]

    _last_used = time.monotonic()  # the idle clock starts after the work
    for row in windows:  # a non-finite score would poison downstream pgvector math
        if not all(math.isfinite(v) for v in row["embedding"]):
            raise RuntimeError("non-finite CLAP embedding")
    return {
        "windows": windows,
        "window_ms": WINDOW_MS,
        "hop_ms": HOP_MS,
        "models": {"tagger": TAGGER_ID, "clap": CLAP_ID},
    }


@app.post("/v1/audio/analyze")
async def analyze(file: UploadFile) -> dict:
    # Only the cold start short-circuits: a "failed" worker must reach
    # _ensure_loaded so the retry/exit bookkeeping runs instead of 503ing forever.
    if _state == "loading":
        raise HTTPException(status_code=503, detail="models are not loaded yet")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty audio file")
    try:
        return await asyncio.to_thread(_analyze, data)
    except ModelsUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("analysis failed")
        raise HTTPException(status_code=500, detail=f"analysis failed: {exc}") from exc


class TextRequest(BaseModel):
    texts: list[str]


@app.post("/v1/text/embeddings")
async def text_embeddings(body: TextRequest) -> dict:
    # Only the cold start short-circuits: a "failed" worker must reach
    # _ensure_loaded so the retry/exit bookkeeping runs instead of 503ing forever.
    if _state == "loading":
        raise HTTPException(status_code=503, detail="models are not loaded yet")
    if not body.texts or not all(t.strip() for t in body.texts):
        raise HTTPException(status_code=400, detail="texts must be non-empty strings")

    def _embed() -> list[list[float]]:
        global _last_used
        import torch

        _, _, _, clap_processor, clap = _ensure_loaded()
        device = next(clap.parameters()).device
        with _gpu_lock, torch.inference_mode():
            inputs = clap_processor(text=body.texts, return_tensors="pt", padding=True)
            inputs = {k: v.to(device) for k, v in inputs.items()}
            out = clap.get_text_features(**inputs)
            embeddings = (out if torch.is_tensor(out) else out.pooler_output).float()
            embeddings = torch.nn.functional.normalize(embeddings, dim=1)
        _last_used = time.monotonic()
        return [[round(v, 6) for v in row] for row in embeddings.tolist()]

    try:
        return {"embeddings": await asyncio.to_thread(_embed), "model": CLAP_ID}
    except ModelsUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("text embedding failed")
        raise HTTPException(status_code=500, detail=f"text embedding failed: {exc}") from exc
