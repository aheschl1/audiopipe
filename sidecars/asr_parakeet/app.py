"""Transcription server: parakeet-tdt-0.6b-v3 + faster-whisper large-v3.

A thin, owned serving layer speaking the standard transcriptions API. Both
models stay resident; pick one per request with ``?model=parakeet|whisper``
(full HF ids also accepted; default parakeet). Word/segment timestamps come
back as clip-relative ms from either model — parakeet's are native TDT,
whisper's are faster-whisper word timestamps.

    POST /v1/audio/transcriptions?model=...   multipart file -> {"text", "words", ...}
    GET  /v1/models                           the loaded model ids
    GET  /health                              503 until BOTH models are loaded;
                                              200 "idle-unloaded" after eviction

Models load in a background thread so the port binds immediately and the
compose healthcheck gates readiness. Inference is serialized with one lock:
one GPU, one request at a time.

With IDLE_UNLOAD_SECONDS set, a watchdog frees the CUDA memory after that
long without inference; the next request blocks while the models reload
(callers must budget their timeout for that, not just a contended GPU).
Whisper is CTranslate2, not torch: its memory frees on object destruction,
untouched by torch.cuda.empty_cache(). With IDLE_UNLOAD_EXIT the watchdog
exits instead — only process death releases the CUDA context itself — and
the restarted process loads lazily, holding zero GPU memory until the first
request.
"""

import asyncio
import gc
import logging
import os
import tempfile
import threading
import time

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import JSONResponse

PARAKEET_ID = os.environ.get("ASR_MODEL", "nvidia/parakeet-tdt-0.6b-v3")
# The A/B contender slot. Off by default since the 2026-08 experiment
# concluded (parakeet won — audio-pipeline docs/asr-ab-results.md); flip on
# to re-run an evaluation.
WHISPER_ENABLED = os.environ.get("ASR_WHISPER_ENABLED", "0") == "1"
WHISPER_ID = os.environ.get("ASR_WHISPER_MODEL", "large-v3")
WHISPER_COMPUTE = os.environ.get("ASR_WHISPER_COMPUTE", "int8_float16")
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("asr")

app = FastAPI(title="asr-parakeet")

_models: dict[str, object] = {}
_load_errors: dict[str, str] = {}
_gpu_lock = threading.Lock()
# Load/unload transitions. The watchdog only tries _gpu_lock without
# blocking while holding _lifecycle, so the two locks cannot deadlock.
_lifecycle = threading.Lock()
_last_used = time.monotonic()
_ever_loaded = False  # a cold start that never worked must not exit-loop
_reload_failures = 0
_retry_after = 0.0
# Set by _pin_timestamp_decoding at load; gates the fast transcribe path.
_parakeet_pinned = False
_state = "loading"  # "loading" | "loaded" | "idle" | "failed"


class ModelsUnavailable(RuntimeError):
    """The requested model is missing and could not be (re)loaded."""


def _expected() -> tuple[str, ...]:
    return ("parakeet", "whisper") if WHISPER_ENABLED else ("parakeet",)


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


def _pin_timestamp_decoding(model) -> bool:
    """Fix the timestamp decoding config now, so transcribe() need not.

    ``EncDecRNNTModel.transcribe(timestamps=...)`` re-runs
    ``change_decoding_strategy`` on every call, rebuilding the TDT decoder per
    clip: ~375 ms of the ~484 ms a short utterance used to cost. Pinning it
    here lets _transcribe_parakeet skip that. Only the RNNT/TDT path is
    pinnable -- any other checkpoint keeps the ordinary per-call route.
    """
    from nemo.collections.asr.models.rnnt_models import EncDecRNNTModel
    from omegaconf import open_dict

    if not isinstance(model, EncDecRNNTModel):
        return False
    try:
        with open_dict(model.cfg.decoding):
            model.cfg.decoding.compute_timestamps = True
            model.cfg.decoding.preserve_alignments = True
        model.change_decoding_strategy(model.cfg.decoding, verbose=False)
    except Exception:
        logger.exception("could not pin timestamp decoding; keeping the per-call path")
        return False
    return True


def _load_models() -> None:
    global _state
    if "parakeet" not in _models:
        try:
            logger.info("loading %s ...", PARAKEET_ID)
            from nemo.collections.asr.models import ASRModel

            model = ASRModel.from_pretrained(PARAKEET_ID).eval().to("cuda")
            global _parakeet_pinned
            _parakeet_pinned = _pin_timestamp_decoding(model)
            _models["parakeet"] = model
            logger.info("parakeet ready (timestamps pinned=%s)", _parakeet_pinned)
        except Exception as exc:
            _load_errors["parakeet"] = f"{type(exc).__name__}: {exc}"
            logger.exception("parakeet load failed")
    if WHISPER_ENABLED and "whisper" not in _models:
        try:
            logger.info("loading whisper %s (%s) ...", WHISPER_ID, WHISPER_COMPUTE)
            from faster_whisper import WhisperModel

            _models["whisper"] = WhisperModel(
                WHISPER_ID, device="cuda", compute_type=WHISPER_COMPUTE
            )
            logger.info("whisper ready")
        except Exception as exc:
            _load_errors["whisper"] = f"{type(exc).__name__}: {exc}"
            logger.exception("whisper load failed")
    _state = "loaded" if all(key in _models for key in _expected()) else "failed"
    _record_load_result()


def _ensure_loaded(key: str) -> dict[str, object]:
    """The models dict, reloading it first if the watchdog evicted it.

    Returns the dict rather than having callers read the global: a reference
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
            _load_errors.clear()
            _load_models()
        if key not in _models:
            raise ModelsUnavailable(_load_errors.get(key, f"{key} is not loaded yet"))
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
            _load_errors.clear()
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

            # Rebound, not cleared: an in-flight reference keeps working.
            _models = {}
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
    if _state == "idle":
        status = {key: "idle-unloaded" for key in _expected()}
        if not WHISPER_ENABLED:
            status["whisper"] = "disabled"
        return JSONResponse(
            {
                "status": "idle-unloaded",
                "models": status,
                "idle_unload_seconds": IDLE_UNLOAD_SECONDS,
            }
        )
    status = {
        key: "ok" if key in _models else _load_errors.get(key, "loading")
        for key in _expected()
    }
    if not WHISPER_ENABLED:
        status["whisper"] = "disabled"
    if all(key in _models for key in _expected()):
        return JSONResponse(
            {"status": "ok", "models": status, "idle_unload_seconds": IDLE_UNLOAD_SECONDS}
        )
    return JSONResponse(
        {"status": "failed" if _state == "failed" else "loading", "models": status},
        status_code=503,
    )


@app.get("/v1/models")
def models() -> dict:
    data = [{"id": PARAKEET_ID, "object": "model", "key": "parakeet"}]
    if WHISPER_ENABLED:
        data.append({"id": WHISPER_ID, "object": "model", "key": "whisper"})
    return {"object": "list", "data": data}


def _resolve(model: str | None) -> str:
    if model in (None, "", "parakeet", PARAKEET_ID):
        return "parakeet"
    if model in ("whisper", WHISPER_ID, f"openai/whisper-{WHISPER_ID}"):
        return "whisper"
    raise HTTPException(status_code=422, detail=f"unknown model {model!r}")


def _transcribe_parakeet(model, path: str) -> dict:
    import torch

    with torch.inference_mode():
        if _parakeet_pinned:
            from nemo.collections.asr.models.rnnt_models import EncDecRNNTModel

            # The parent's transcribe, deliberately: the RNNT override exists
            # only to re-pin the decoding strategy, which _pin_timestamp_
            # decoding already did. timestamps=True still rides along so
            # process_timestamp_outputs fills hyp.timestamp.
            (hyp,) = super(EncDecRNNTModel, model).transcribe(
                audio=[path],
                batch_size=1,
                return_hypotheses=True,
                timestamps=True,
                verbose=False,
            )
        else:
            (hyp,) = model.transcribe([path], timestamps=True, verbose=False)
    stamps = getattr(hyp, "timestamp", None) or {}
    out = {
        "text": (hyp.text or "").strip(),
        "model": PARAKEET_ID,
        "words": [
            {"word": w["word"], "start_ms": int(w["start"] * 1000), "end_ms": int(w["end"] * 1000)}
            for w in stamps.get("word", [])
        ],
        "segments": [
            {"text": s["segment"], "start_ms": int(s["start"] * 1000), "end_ms": int(s["end"] * 1000)}
            for s in stamps.get("segment", [])
        ],
    }
    lang = getattr(hyp, "langs", None) or getattr(hyp, "language", None)
    if isinstance(lang, list) and lang and all(isinstance(item, str) for item in lang):
        lang = max(set(lang), key=lang.count)
    if isinstance(lang, str):
        out["language"] = lang
    return out


def _transcribe_whisper(model, path: str) -> dict:
    segments, info = model.transcribe(
        path,
        word_timestamps=True,
        vad_filter=True,
        condition_on_previous_text=False,
    )
    words, segs, texts = [], [], []
    for seg in segments:
        text = seg.text.strip()
        if text:
            texts.append(text)
        segs.append(
            {"text": text, "start_ms": int(seg.start * 1000), "end_ms": int(seg.end * 1000)}
        )
        for w in seg.words or []:
            words.append(
                {"word": w.word.strip(), "start_ms": int(w.start * 1000), "end_ms": int(w.end * 1000)}
            )
    return {
        "text": " ".join(texts),
        "model": WHISPER_ID,
        "language": info.language,
        "words": words,
        "segments": segs,
    }


def _transcribe_file(path: str, key: str) -> dict:
    global _last_used
    models = _ensure_loaded(key)
    with _gpu_lock:
        if key == "whisper":
            result = _transcribe_whisper(models["whisper"], path)
        else:
            result = _transcribe_parakeet(models["parakeet"], path)
    _last_used = time.monotonic()  # the idle clock starts after the work
    return result


@app.post("/v1/audio/transcriptions")
async def transcriptions(file: UploadFile, model: str | None = None) -> dict:
    key = _resolve(model)
    if key == "whisper" and not WHISPER_ENABLED:
        raise HTTPException(status_code=422, detail="whisper is disabled (ASR_WHISPER_ENABLED=1)")
    # Only the cold start short-circuits here: a "failed" worker must reach
    # _ensure_loaded so the retry/exit bookkeeping runs instead of 503ing forever.
    if _state == "loading" and key not in _models:
        raise HTTPException(status_code=503, detail=f"{key} is not loaded yet")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty audio file")

    suffix = os.path.splitext(file.filename or "")[1] or ".wav"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
        handle.write(data)
        path = handle.name
    try:
        return await asyncio.to_thread(_transcribe_file, path, key)
    except ModelsUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("transcription failed")
        raise HTTPException(status_code=500, detail=f"transcription failed: {exc}") from exc
    finally:
        os.unlink(path)
