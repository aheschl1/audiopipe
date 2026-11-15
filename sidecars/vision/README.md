# vision

Serving container for the context's **keyframe** tier and text→frame search:
image and text embeddings in one joint space.

- **Model:** [`google/siglip2-so400m-patch16-384`](https://huggingface.co/google/siglip2-so400m-patch16-384)
  (Apache-2.0), 1152-d L2-normalized embeddings, about 2.5 GB VRAM in fp16.

| Route | Meaning |
|---|---|
| `POST /v1/images/embeddings` | multipart `files` (any number of JPEG/PNG) → `{"embeddings": [[...]], "model"}` |
| `POST /v1/text/embeddings` | `{"texts": [...]}` (1–64) → same shape |
| `GET /v1/models` | the model id |
| `GET /health` | always 200; `state` is `loaded` or `idle` |

The model loads on the first request (about 6 s from a warm cache) and the
process exits after `IDLE_EXIT_SECONDS` without one (the deploy sets 600). The
restart policy brings back an unloaded process, so an idle sidecar holds no GPU
memory and never reloads in-process. Requests are serialized; throughput comes
from batching inside a request (`BATCH`, default 32).

Env knobs: `VISION_MODEL`, `BATCH`, `IDLE_EXIT_SECONDS` (0 = stay loaded).
