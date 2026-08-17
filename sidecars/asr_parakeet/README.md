# asr_parakeet

Two ASR models behind the standard transcriptions API, selected per request
with `?model=parakeet|whisper` (default parakeet):

- **nvidia/parakeet-tdt-0.6b-v3** (~3.2 GB VRAM) — the production model:
  native TDT word/segment timestamps, 25 European languages with auto-ID,
  no LLM decoder to hallucinate on noisy/short clips.
- **faster-whisper large-v3** int8_float16 (~3 GB) — the A/B contender
  slot, **disabled by default** (`ASR_WHISPER_ENABLED=1` to load it; weights
  re-download on boot). The 2026-08 experiment concluded parakeet·chunk wins
  (audio-pipeline `docs/asr-ab-results.md`). Served with `vad_filter=True` +
  `condition_on_previous_text=False` when enabled.

Consumed by the audio-pipeline's `transcribe` tier (parakeet) and
`transcribe-ab` tier (both), via
`PROCESSING_TRANSCRIBER_BASE_URL=http://127.0.0.1:8033/v1`.

```bash
# make sidecars starts all three; this is just one of them
docker compose -f deploy/docker-compose.yml up -d --build asr-parakeet

# smoke test (first boot downloads weights; /health is 503 until BOTH load)
curl -s http://127.0.0.1:8033/health
curl -s -F file=@clip.wav http://127.0.0.1:8033/v1/audio/transcriptions
curl -s -F file=@clip.wav 'http://127.0.0.1:8033/v1/audio/transcriptions?model=whisper'
```

- Env: `ASR_MODEL` (parakeet id), `ASR_WHISPER_MODEL` (`large-v3`),
  `ASR_WHISPER_COMPUTE` (`int8_float16`), `IDLE_UNLOAD_SECONDS` (0 = keep
  resident; the deploy sets 1800)
- With `IDLE_UNLOAD_SECONDS` set, the models are dropped from CUDA after that
  long without inference; the next request **blocks** while they reload from
  the cache. `/health` stays 200 (`"idle-unloaded"`) while evicted. Whisper is
  CTranslate2, not torch: its memory frees on object destruction, and
  `torch.cuda.empty_cache()` cannot touch it. The process's CUDA context
  (~0.5 GB) stays resident either way — only weights are reclaimed.
- Weights cache in the `asr-parakeet-hf-cache` volume.
- Response: `{"text", "model", "language"?, "words": [{"word", "start_ms",
  "end_ms"}], "segments": [{"text", "start_ms", "end_ms"}]}` — timestamps
  clip-relative ms; the pipeline converts to session-absolute.
- One request at a time by design (single GPU, shared lock).
