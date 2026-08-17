# diar_pyannote

**BUT-FIT/diarizen-wavlm-large-s80-md-v2** behind a small JSON contract, plus
embeddings so downstream clustering never re-runs diarization.

(The directory, container and volume names still say `pyannote`; renaming them
would churn compose, the Makefile and the cache volume for no benefit. The
diarizer is DiariZen.)

DiariZen replaces pyannote's SincNet+LSTM segmentation with WavLM Base+ and
Conformer blocks, which is where its accuracy comes from: AMI 22.4 → 14.0 DER,
AISHELL-4 12.2 → 9.8, AliMeeting 24.4 → 12.5 against pyannote 3.1. It keeps the
same WeSpeaker **ResNet34-LM** embedder and the same VBx clustering that
pyannote community-1 uses, so voice-prints stay in the same 256-d space and the
tier's clustering thresholds carry over unchanged.

## Embeddings

- **Per-turn embeddings**, computed here from each turn's audio with
  overlapping speech masked out. These let the caller audit a label's purity —
  a merged label's blended aggregate can't reveal that it holds several
  people — and build voice-prints free of crosstalk. Turns also carry
  `overlap_ms` / `clean_ms` (time shared with / free of other speakers).
- **Per-speaker aggregates** (`embeddings`), a fallback only: computed for
  labels that produced no usable per-turn vector, by embedding that label's
  audio unmasked.

Overlap is excluded by masking samples rather than by splicing the clean parts
together — the embedder drops masked frames after computing filterbanks, so no
seam discontinuities reach the features. Turns with under `EMBED_MIN_CLEAN_MS`
(env, default 1000) of clean audio get `"embedding": null`; short crops embed
unreliably.

DiariZen returns a bare `Annotation`, so embedding is owned here rather than
borrowed from pipeline internals.

Consumed by the audio-pipeline's `diarize` tier
(`PROCESSING_DIARIZER_BASE_URL=http://127.0.0.1:8034/v1`).

## Setup

1. Hugging Face account: accept the conditions on
   `pyannote/wespeaker-voxceleb-resnet34-LM` (the embedder is gated; the
   DiariZen weights are not).
2. Set `HF_TOKEN=hf_...` (a read token) in `deploy/.env`.

```bash
make sidecars                     # or, just this one:
docker compose -f deploy/docker-compose.yml up -d --build diar-pyannote

curl -s http://127.0.0.1:8034/health
curl -s -F file=@meeting.wav http://127.0.0.1:8034/v1/audio/diarizations | python3 -m json.tool
```

- `DIAR_MODEL` (default `BUT-FIT/diarizen-wavlm-large-s80-md-v2`). The `-v2`
  model handles up to 4 overlapping speakers; the non-v2 large model handles 3.
  `BUT-FIT/diarizen-wavlm-base-s80-md` is the cheaper fallback.
- `EMBED_MODEL` (default `pyannote/wespeaker-voxceleb-resnet34-LM`) — the
  embedder for the vectors this service *exports*. DiariZen hardcodes this same
  model for its own internal clustering, so overriding this changes voice-prints
  only, never how DiariZen assigns labels. Changing it also changes the vector
  space, invalidating the tier's clustering thresholds and every stored
  voice-print.
- `DIAR_BATCH_SIZE` (default 8; 0 keeps the model card's own 32). This is the
  load-bearing memory knob — see below.
- `IDLE_UNLOAD_SECONDS` (0 = keep resident; the deploy sets 1800) — drop the
  pipeline and embedder from CUDA after that long without inference. The next
  request **blocks** while they reload (minutes, from the cache volume);
  `PROCESSING_DIARIZER_TIMEOUT_SECONDS` (900) covers it. `/health` stays 200
  (`"idle-unloaded"`) while evicted so the compose gate never flaps.
- Weights cache in the `diar-pyannote-hf-cache` volume. The `-s80` checkpoints
  are 80% structurally pruned (WavLM Large 316.6M → 63.3M params), so the
  segmentation weights are 278 MB.
- **VRAM, measured on a 3090** (peak for this container, 30s-clip audio looped
  to length):

  | batch | 10-min block | 30-min block | time (30-min) |
  |-------|--------------|--------------|---------------|
  | 32 (model card) | 13,986 MiB | not run — would not fit | — |
  | 8 (default here) | 3,878 MiB | 3,880 MiB | 61 s |

  At 32 the peak grows with block length and a 30-minute block does not fit
  alongside the other GPU services. At 8 it is flat — batch-bounded, not
  length-bounded — for about 10% more wall time, and lands *below* the ~10 GB
  the old pyannote sidecar peaked at. Raise it only if the GPU is otherwise
  idle, and re-measure before raising `diarize_max_block_ms`.
- One request at a time (single GPU); the pipeline sends one clip per speech
  *block* (contiguous speech, ≤30 min) — never a whole session.
- Speaker labels are local to one request; identity across blocks/sessions is
  the clustering tier's job, fed by the returned embeddings.

## Licensing

DiariZen's **source is MIT** but its **model weights are CC-BY-NC-4.0** —
research and personal use only. This is a blocker for any commercial use of
the pipeline.

## Build notes

DiariZen pins python 3.10 and torch 2.1.1 and vendors its own pyannote.audio
fork in-tree, so this image is built from a pinned git commit rather than a pip
release, and runs older torch than the rest of the stack. Both are contained
here. `requirements.txt` is installed whole, including inference-irrelevant
packages, because the package declares no runtime dependencies of its own —
worth trimming once the real import surface is known.
