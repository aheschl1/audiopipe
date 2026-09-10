"""Speaker-diarization server for DiariZen (WavLM + Conformer segmentation).

The owned serving layer for the audio-pipeline's diarize tier. DiariZen does
the segmentation and clustering; this file speaks a small JSON contract so the
tier (or any HTTP client) can be pointed at a different diarizer later without
code changes.

    POST /v1/audio/diarizations   multipart file ->
        {"turns": [{"start_ms", "end_ms", "speaker",
                    "overlap_ms": 120,        # time shared with other speakers
                    "clean_ms": 2400,         # non-overlapped time
                    "embedding": [floats] | null}, ...],
         "embeddings": {"SPEAKER_00": [floats], ...},
         "model": "...", "embedding_model": "..."}
    GET  /v1/models               the loaded model id
    GET  /health                  503 while the model is still loading;
                                  200 "idle-unloaded" after an idle eviction

Per-turn ``embedding`` is computed with overlapping speech masked out (turns
under ``EMBED_MIN_CLEAN_MS`` of clean audio get ``null``); per-label
``embeddings`` is a fallback for labels with no usable per-turn vector,
embedded unmasked. Rationale in README.md.

The model loads in a background thread so the port binds immediately and the
compose healthcheck gates readiness. Inference is serialized with a lock: one
GPU, one request at a time.

With IDLE_UNLOAD_SECONDS set, a watchdog frees the CUDA memory after that
long without inference; the next request blocks while the model reloads
(minutes from the HF cache — callers must budget their timeout for that).
With IDLE_UNLOAD_EXIT the watchdog exits instead — only process death
releases the CUDA context itself — and the restarted process loads lazily,
holding zero GPU memory until the first request.
"""

import asyncio
import gc
import logging
import math
import os
import tempfile
import threading
import time

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import JSONResponse

MODEL_ID = os.environ.get("DIAR_MODEL", "BUT-FIT/diarizen-wavlm-large-s80-md-v2")
# The embedder for the vectors this service exports. Changing it changes the
# vector space, invalidating stored voice-prints and clustering thresholds.
EMBED_MODEL = os.environ.get("EMBED_MODEL", "pyannote/wespeaker-voxceleb-resnet34-LM")
# Turns with less clean (non-overlapped) audio than this get no per-turn
# embedding: the vectors degrade sharply on very short crops.
EMBED_MIN_CLEAN_MS = int(os.environ.get("EMBED_MIN_CLEAN_MS", "1000"))
# Segmentation/embedding batch. 0 keeps the model card's own value.
BATCH_SIZE = int(os.environ.get("DIAR_BATCH_SIZE", "8"))
# Release CUDA memory after this long without inference; 0 keeps the model
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("diar")

app = FastAPI(title="diar")

_pipeline = None
_embedding = None
_audio = None
_load_error: str | None = None
_gpu_lock = threading.Lock()
# Load/unload transitions. The helpers below read the module globals, so the
# reload check runs inside _gpu_lock (_diarize_file): the watchdog needs that
# lock to evict, so the globals cannot vanish mid-inference. No deadlock: the
# watchdog only ever tries _gpu_lock without blocking.
_lifecycle = threading.Lock()
_last_used = time.monotonic()
_ever_loaded = False  # a cold start that never worked must not exit-loop
_reload_failures = 0
_retry_after = 0.0
_state = "loading"  # "loading" | "loaded" | "idle" | "failed"


class ModelsUnavailable(RuntimeError):
    """The model is missing and could not be (re)loaded."""


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


def _load_model() -> None:
    global _pipeline, _embedding, _audio, _load_error, _state, _last_used
    try:
        token = os.environ.get("HF_TOKEN")
        if not token:
            raise RuntimeError("HF_TOKEN is not set (the embedding weights are gated)")
        import torch
        from diarizen.pipelines.inference import DiariZenPipeline
        from huggingface_hub import hf_hub_download
        from pyannote.audio import Audio
        from pyannote.audio.pipelines.speaker_verification import (
            PretrainedSpeakerEmbedding,
        )

        device = torch.device("cuda")

        logger.info("loading %s ...", MODEL_ID)
        pipeline = DiariZenPipeline.from_pretrained(MODEL_ID)
        if pipeline is None:
            raise RuntimeError("DiariZenPipeline.from_pretrained returned None")
        if hasattr(pipeline, "to"):
            pipeline.to(device)
        if BATCH_SIZE:
            # The model card's 32 peaks ~14 GB and grows with block length.
            pipeline.segmentation_batch_size = BATCH_SIZE
            pipeline.embedding_batch_size = BATCH_SIZE

        logger.info("loading %s ...", EMBED_MODEL)
        # Resolved to a local file first: handed a repo id, the vendored
        # pyannote fork calls hf_hub_download(use_auth_token=...), a kwarg
        # current huggingface_hub has removed.
        weights = hf_hub_download(repo_id=EMBED_MODEL, filename="pytorch_model.bin")
        embedding = PretrainedSpeakerEmbedding(weights, device=device)

        _embedding = embedding
        _audio = Audio(sample_rate=embedding.sample_rate, mono="downmix")
        # Last: /health and the request path gate readiness on _pipeline.
        _pipeline = pipeline
        _load_error = None
        _state = "loaded"
        _last_used = time.monotonic()
        logger.info("model ready (pipeline=%s)", type(pipeline).__name__)
    except Exception as exc:  # surfaced via /health; the container stays up
        _load_error = f"{type(exc).__name__}: {exc}"
        _state = "failed"
        logger.exception("model load failed")
    _record_load_result()


def _ensure_loaded() -> None:
    """Reload after an idle unload; call while holding ``_gpu_lock``.

    A failed reload leaves the state ``failed``, never ``idle``: the idle
    branch of /health is a deliberate 200, and answering green while every
    request 503s is how a wedged reload goes unnoticed for days.
    """
    global _last_used
    with _lifecycle:
        if _state in ("idle", "failed") and time.monotonic() >= _retry_after:
            logger.info("(re)loading model (state=%s) ...", _state)
            _load_model()
        if _state != "loaded":
            raise ModelsUnavailable(_load_error or "model is not loaded yet")
        _last_used = time.monotonic()


def _idle_watchdog() -> None:
    while True:
        time.sleep(_WATCHDOG_TICK_SECONDS)
        _watchdog_tick()


def _watchdog_tick() -> None:
    global _pipeline, _embedding, _audio, _state
    with _lifecycle:
        if _state == "failed" and _ever_loaded and time.monotonic() >= _retry_after:
            # A wedged reload must not wait for traffic: the 2026-08-23
            # outage stalled at 4/5 failures when the queue went quiet, so
            # the watchdog walks the failure count to _die by itself.
            logger.info("watchdog retrying failed (re)load ...")
            _load_model()
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

            _pipeline = None
            _embedding = None
            _audio = None
            gc.collect()
            torch.cuda.empty_cache()
            _state = "idle"
            logger.info("idle for %ss; model unloaded", IDLE_UNLOAD_SECONDS)
        finally:
            _gpu_lock.release()


def _load_model_locked() -> None:
    # Under _lifecycle: a request arriving mid-startup can now trigger its own
    # reload off the "failed" state, and the two must not load side by side.
    with _lifecycle:
        _load_model()


@app.on_event("startup")
def _startup() -> None:
    global _state
    if IDLE_UNLOAD_EXIT:
        _state = "idle"  # lazy: zero GPU until the first request loads
    else:
        threading.Thread(target=_load_model_locked, daemon=True).start()
    if IDLE_UNLOAD_SECONDS > 0:
        threading.Thread(target=_idle_watchdog, daemon=True).start()


@app.get("/health")
def health() -> JSONResponse:
    # An idle unload is deliberate, so it stays 200: compose gates the worker
    # on this endpoint, and an evicted-but-healthy container must not flap it.
    if _state == "idle":
        return JSONResponse(
            {
                "status": "idle-unloaded",
                "model": MODEL_ID,
                "embedding_model": EMBED_MODEL,
                "idle_unload_seconds": IDLE_UNLOAD_SECONDS,
            }
        )
    if _pipeline is not None:
        return JSONResponse(
            {
                "status": "ok",
                "model": MODEL_ID,
                "embedding_model": EMBED_MODEL,
                "pipeline_class": type(_pipeline).__name__,
                "per_turn_embeddings": _embedding is not None and _audio is not None,
                "idle_unload_seconds": IDLE_UNLOAD_SECONDS,
            }
        )
    body = {"status": "loading" if _load_error is None else "failed"}
    if _load_error is not None:
        body["error"] = _load_error
    return JSONResponse(body, status_code=503)


@app.get("/v1/models")
def models() -> dict:
    return {"object": "list", "data": [{"id": MODEL_ID, "object": "model"}]}


def _crop(path: str, segment, duration_s: float):
    """The segment's waveform clamped to the file (turn boundaries can overrun
    it by a frame, and Audio.crop raises past the end); None when empty."""
    from pyannote.core import Segment

    clamped = Segment(max(segment.start, 0.0), min(segment.end, duration_s))
    if clamped.duration <= 0:
        return None, None
    waveform, sample_rate = _audio.crop(path, clamped)
    return waveform, (clamped, sample_rate)


def _vector(waveform, masks=None):
    """Run the embedder, returning a plain float list or None if not finite."""
    import numpy

    values = _embedding(waveform.unsqueeze(0), masks=masks)[0]
    if not bool(numpy.isfinite(values).all()):
        return None
    return [float(value) for value in values]


def _turn_embedding(path: str, segment, clean_parts, duration_s: float):
    """Embed one turn with overlapped speech masked out; None when unusable."""
    import torch
    from pyannote.core import Segment

    waveform, meta = _crop(path, segment, duration_s)
    if waveform is None:
        return None
    clamped, sample_rate = meta

    mask = torch.zeros(waveform.shape[1])
    for part in clean_parts:
        kept = Segment(
            max(part.start, clamped.start), min(part.end, clamped.end)
        )
        if kept.duration <= 0:
            continue
        start = int(round((kept.start - clamped.start) * sample_rate))
        end = int(round((kept.end - clamped.start) * sample_rate))
        mask[start:end] = 1.0
    if not bool(mask.any()):
        return None
    return _vector(waveform, masks=mask.unsqueeze(0))


def _pool(vectors, weights):
    """Weighted mean of unit-normalized vectors, re-normalized; None if empty."""
    import numpy

    if not vectors:
        return None
    matrix = numpy.asarray(vectors, dtype=float)
    norms = numpy.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    pooled = ((matrix / norms) * numpy.asarray(weights, dtype=float)[:, None]).sum(
        axis=0
    )
    length = float(numpy.linalg.norm(pooled))
    if length == 0.0:
        return None
    return [float(value) for value in pooled / length]


def _label_embedding(path: str, segments, duration_s: float):
    """Duration-weighted pooled embedding over a label's unmasked audio; the
    fallback for labels with no usable per-turn vector."""
    # No clean-audio floor here, and the embedder errors on too-short crops.
    min_samples = getattr(_embedding, "min_num_samples", 0) or 0

    vectors, weights = [], []
    for segment in segments:
        waveform, meta = _crop(path, segment, duration_s)
        if waveform is None or waveform.shape[1] < min_samples:
            continue
        values = _vector(waveform)
        if values is None:
            continue
        vectors.append(values)
        weights.append(max(meta[0].duration, 1e-6))
    return _pool(vectors, weights)


def _diarize_file(path: str) -> dict:
    global _last_used
    from pyannote.core import Timeline

    with _gpu_lock:
        _ensure_loaded()
        try:
            diarization = _pipeline(path)
        except ValueError as exc:
            # An all-silent block makes reconstruct() size an array with
            # max(hard_clusters) + 1 == -1. The upstream VAD gate is
            # high-recall, so silence is an answer, not a failure.
            if "negative dimensions" not in str(exc):
                raise
            logger.warning("no speakers detected; returning an empty diarization")
            _last_used = time.monotonic()
            return {
                "turns": [],
                "embeddings": {},
                "model": MODEL_ID,
                "embedding_model": EMBED_MODEL,
            }
        # DiariZen labels are bare integers; the contract and the purity
        # audit's SPEAKER_XX.N sub-label grammar expect SPEAKER_XX.
        # labels() is sorted, so the mapping is deterministic.
        diarization = diarization.rename_labels(
            {label: f"SPEAKER_{index:02d}"
             for index, label in enumerate(diarization.labels())}
        )
        overlap = diarization.get_overlap()
        duration_s = _audio.get_duration(path)

        turns = []
        segments_by_label: dict[str, list] = {}
        clean_by_label: dict[str, tuple[list, list]] = {}
        for segment, _, label in diarization.itertracks(yield_label=True):
            clean_parts = Timeline([segment]).extrude(overlap)
            clean_ms = int(round(sum(part.duration for part in clean_parts) * 1000))
            turn_ms = int(round(segment.duration * 1000))
            embedding = (
                _turn_embedding(path, segment, clean_parts, duration_s)
                if clean_ms >= EMBED_MIN_CLEAN_MS
                else None
            )
            segments_by_label.setdefault(label, []).append(segment)
            vectors, weights = clean_by_label.setdefault(label, ([], []))
            if embedding is not None:
                vectors.append(embedding)
                weights.append(max(clean_ms, 1))
            turns.append(
                {
                    "start_ms": int(segment.start * 1000),
                    "end_ms": int(math.ceil(segment.end * 1000)),
                    "speaker": label,
                    "overlap_ms": max(turn_ms - clean_ms, 0),
                    "clean_ms": clean_ms,
                    "embedding": embedding,
                }
            )

        # Always present, so the caller's fallback never depends on its own
        # clean-audio gate matching EMBED_MIN_CLEAN_MS.
        by_speaker = {}
        for label, segments in segments_by_label.items():
            vectors, weights = clean_by_label[label]
            vector = _pool(vectors, weights) or _label_embedding(
                path, segments, duration_s
            )
            if vector is not None:
                by_speaker[label] = vector

    _last_used = time.monotonic()  # the idle clock starts after the work
    return {
        "turns": turns,
        "embeddings": by_speaker,
        "model": MODEL_ID,
        "embedding_model": EMBED_MODEL,
    }


@app.post("/v1/audio/diarizations")
async def diarizations(file: UploadFile) -> dict:
    # Only the cold start short-circuits: a "failed" worker must reach
    # _ensure_loaded so the retry/exit bookkeeping runs instead of 503ing forever.
    if _state == "loading":
        raise HTTPException(status_code=503, detail="model is not loaded yet")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty audio file")

    suffix = os.path.splitext(file.filename or "")[1] or ".wav"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
        handle.write(data)
        path = handle.name
    try:
        return await asyncio.to_thread(_diarize_file, path)
    except ModelsUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("diarization failed")
        raise HTTPException(status_code=500, detail=f"diarization failed: {exc}") from exc
    finally:
        os.unlink(path)
