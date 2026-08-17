# audio-tagger

Serving container for the audio-pipeline's **audio-tag** tier: sound-event
tagging + audio embeddings in one service, because both consume the same
windows of audio.

- **Tagger:** [`mispeech/ced-base`](https://huggingface.co/mispeech/ced-base)
  (Apache-2.0) — AudioSet's 527 classes, per-window sigmoid scores. Chosen
  over AST for accuracy (50.0 vs 45.9 mAP as single checkpoints) at the same
  size (~180 MB VRAM in fp16).
- **Embedder:** [`laion/larger_clap_general`](https://huggingface.co/laion/larger_clap_general)
  (Apache-2.0) — joint audio-text space; the per-window audio embeddings the
  pipeline stores in pgvector, and the text embeddings that make them
  searchable ("keyboard typing" → matching windows).

## API

| Route | Meaning |
|---|---|
| `POST /v1/audio/analyze` | multipart WAV → per-window `{start_ms, end_ms, labels:[{label,score}], embedding}` (windowing 10 s / 5 s hop happens here; times are clip-relative) |
| `POST /v1/text/embeddings` | `{"texts": [...]}` → CLAP text embeddings (for search queries) |
| `GET /v1/models` | both model ids |
| `GET /health` | 503 until both models are loaded; 200 `"idle-unloaded"` after an idle eviction |

Env knobs: `TAGGER_MODEL`, `CLAP_MODEL`, `WINDOW_MS` (10000), `HOP_MS` (5000),
`BATCH` (16), `TOP_K` (20), `MIN_WINDOW_MS` (2000), `IDLE_UNLOAD_SECONDS`
(0 = keep resident; the deploy sets 1800).

With `IDLE_UNLOAD_SECONDS` set, both models are dropped from CUDA after that
long without inference and reload on the next request — which **blocks** for
the reload (tens of seconds from the cache) instead of erroring. Callers must
budget their timeout for that; the API's search path does via
`API_CLASSIFIER_TIMEOUT_SECONDS`. Health stays 200 while evicted so the
compose gate never flaps.

## Deploy

Wired into `deploy/docker-compose.yml` as `audio-tagger` on
`127.0.0.1:8035` (8033 = ASR, 8034 = diarizer):

```bash
docker compose -f deploy/docker-compose.yml up -d --build audio-tagger
curl http://127.0.0.1:8035/health
```

First start downloads both models into the `audio-tagger-hf-cache` volume;
the healthcheck's `start_period` covers this.

Smoke test:

```bash
curl -s -F file=@clip.wav http://127.0.0.1:8035/v1/audio/analyze | python3 -m json.tool | head
curl -s -X POST http://127.0.0.1:8035/v1/text/embeddings \
     -H 'content-type: application/json' -d '{"texts": ["dog barking"]}' | head -c 200
```
