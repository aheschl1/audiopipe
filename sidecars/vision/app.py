"""Image and text embeddings in SigLIP 2's joint space.

    POST /v1/images/embeddings   multipart ``files`` (JPEG/PNG, many) ->
        {"embeddings": [[floats], ...], "model": "..."}     # L2-normalized
    POST /v1/text/embeddings     {"texts": [...]} -> same shape
    GET  /v1/models              {"model": "..."}
    GET  /health                 always 200; ``state`` says loaded or idle

The model loads lazily on the first request and the process exits after
IDLE_EXIT_SECONDS without one, so an idle sidecar holds no GPU memory. The
restart policy brings back a fresh, unloaded process.
"""

import io
import logging
import os
import threading
import time

from fastapi import FastAPI, HTTPException, UploadFile
from pydantic import BaseModel

MODEL_ID = os.environ.get("VISION_MODEL", "google/siglip2-so400m-patch16-384")
BATCH = int(os.environ.get("BATCH", "32"))
IDLE_EXIT_SECONDS = int(os.environ.get("IDLE_EXIT_SECONDS", "0"))
MAX_TEXTS = 64

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("vision")

app = FastAPI(title="vision")

_model = None  # (processor, model, device, dtype)
_lock = threading.Lock()
_last_used = time.monotonic()


def _load():
    global _model
    if _model is not None:
        return _model
    import torch
    from transformers import AutoModel, AutoProcessor

    device = "cuda" if torch.cuda.is_available() else "cpu"
    dtype = torch.float16 if device == "cuda" else torch.float32
    started = time.monotonic()
    processor = AutoProcessor.from_pretrained(MODEL_ID)
    model = AutoModel.from_pretrained(MODEL_ID, torch_dtype=dtype).to(device).eval()
    logger.info("loaded %s on %s in %.1fs", MODEL_ID, device, time.monotonic() - started)
    _model = (processor, model, device, dtype)
    return _model


def _run(fn):
    """Serialize GPU work and keep the idle clock fresh."""
    global _last_used
    with _lock:
        try:
            loaded = _load()
        except Exception as exc:
            logger.exception("model load failed")
            raise HTTPException(503, f"model unavailable: {exc}") from exc
        try:
            return fn(*loaded)
        except Exception as exc:
            # Inference faults (CUDA OOM on the shared GPU) are the service's,
            # not the input's: 503 lets the worker refund the attempt.
            logger.exception("inference failed")
            raise HTTPException(503, f"inference failed: {exc}") from exc
        finally:
            _last_used = time.monotonic()


def _normalized(features) -> list[list[float]]:
    import torch

    if not isinstance(features, torch.Tensor):  # transformers 5 returns an output object
        features = features.pooler_output
    features = torch.nn.functional.normalize(features.float(), dim=-1)
    return features.cpu().tolist()


def _decode(blobs: list[bytes]) -> list:
    from PIL import Image

    return [Image.open(io.BytesIO(b)).convert("RGB") for b in blobs]


def _embed_images(images: list) -> list[list[float]]:
    import torch

    def run(processor, model, device, dtype):
        out: list[list[float]] = []
        with torch.inference_mode():
            for i in range(0, len(images), BATCH):
                inputs = processor(images=images[i : i + BATCH], return_tensors="pt")
                pixels = inputs["pixel_values"].to(device, dtype)
                out += _normalized(model.get_image_features(pixel_values=pixels))
        return out

    return _run(run)


def _embed_texts(texts: list[str]) -> list[list[float]]:
    import torch

    def run(processor, model, device, dtype):
        # SigLIP was trained on max_length padding; dynamic padding degrades it.
        inputs = processor(
            text=texts, padding="max_length", max_length=64, truncation=True, return_tensors="pt"
        ).to(device)
        with torch.inference_mode():
            return _normalized(model.get_text_features(**inputs))

    return _run(run)


def _idle_watchdog() -> None:
    while True:
        time.sleep(15)
        if _model is not None and time.monotonic() - _last_used > IDLE_EXIT_SECONDS:
            if _lock.acquire(blocking=False):
                logger.info("idle for %ss; exiting to release the GPU", IDLE_EXIT_SECONDS)
                os._exit(0)


@app.on_event("startup")
def _startup() -> None:
    if IDLE_EXIT_SECONDS > 0:
        threading.Thread(target=_idle_watchdog, daemon=True).start()


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "state": "loaded" if _model is not None else "idle", "model": MODEL_ID}


@app.get("/v1/models")
def models() -> dict:
    return {"model": MODEL_ID}


@app.post("/v1/images/embeddings")
def images(files: list[UploadFile]) -> dict:
    if not files:
        raise HTTPException(422, "no files")
    try:
        images = _decode([f.file.read() for f in files])
    except Exception as exc:
        raise HTTPException(422, f"undecodable image: {exc}") from exc
    return {"embeddings": _embed_images(images), "model": MODEL_ID}


class TextRequest(BaseModel):
    texts: list[str]


@app.post("/v1/text/embeddings")
def texts(body: TextRequest) -> dict:
    if not body.texts or len(body.texts) > MAX_TEXTS:
        raise HTTPException(422, f"send 1-{MAX_TEXTS} texts")
    return {"embeddings": _embed_texts(body.texts), "model": MODEL_ID}
