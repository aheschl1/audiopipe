# Processing pipelines

Background workers that act on recorded audio: transcription, transcript
post-processing, agent triggers - anything derived from a session after (or
beside) capture. This document is the contract; the code lives in
`processing/`.

```
                 ┌───────────────┐
   clients ───▶  │   api (HTTP/  │
                 │   websocket)  │──── writes sessions / segments ───┐
                 └───────────────┘                                   ▼
                                                     ┌────────────────────────┐
                 ┌───────────────┐                   │  Postgres + blob store │
                 │  processing   │  discover/claim   │  processing_jobs       │
                 │  supervisor   │ ◀───────────────▶ │  pipeline_artifacts    │
                 │  ├ session-stats (child process)  │  {pipeline}/... blobs  │
                 │  └ stats-echo    (child process)  └────────────────────────┘
                 └───────────────┘
```

The API and the workers share nothing but the stores. There is **zero
enqueue wiring in the API**: workers find their own work by querying the same
database the API writes.

Run them with `make worker` (`uv run python -m processing`), or a subset with
`--only <name>` (repeatable). `audio-pipeline-worker` is the script entry.

## Topology

- One **supervisor** process (`processing/supervisor.py`) spawns one
  long-lived **child process per registered pipeline** and restarts children
  that die (backoff 1 s doubling to 30 s, reset after 60 s of clean running).
- Each child runs one `Pipeline` instance in its own asyncio loop
  (`processing/worker.py`): discover → drain → wait, forever.
- **Invariant: exactly one worker process per pipeline.** Orphan recovery
  (below) depends on it. Scaling one pipeline to N workers first requires
  claim leases/heartbeats - do not just start the supervisor twice.

## Jobs: lifecycle and the queue

Work is materialized in `processing_jobs` so that retry, backoff,
dead-lettering, priority, and chaining all have one home.

```
queued ──claim──▶ running ──▶ succeeded            (terminal)
   ▲                 │
   │                 ├──▶ queued again (retry: error recorded,
   └─────────────────┘        run_at pushed by base·2^(attempt-1), capped)
                     └──▶ dead   (attempts == max_attempts; terminal)
```

- `claim` takes the next `queued` row with `run_at <= now()`, ordered by
  `priority DESC, run_at, id`, under `FOR UPDATE SKIP LOCKED`; it bumps
  `attempts` at claim time so crashes still count against `max_attempts`.
- The claim transaction never spans `process()`: claiming commits first, the
  outcome lands in a second transaction.
- **Orphan recovery**: at boot a worker requeues every `running` row of its
  pipeline - under the one-worker invariant those can only belong to a dead
  predecessor.
- `pg_notify('processing_jobs', <pipeline>)` fires on every committed
  enqueue. NOTIFY is a latency optimization only - polling (the
  `PROCESSING_POLL_INTERVAL_SECONDS` pass cadence) is the source of truth, so
  jobs enqueued while a worker is down are simply picked up at the next boot
  or pass.

## Self-discovery

Pipelines are responsible for their own querying. Each pipeline implements
`discover(limit) -> Sequence[JobCreate]` - a query it owns, living in
`database/repos/pipelines/<pipeline>.py` (one module per pipeline; classes
extend `BaseRepo`; constructed by the pipeline itself, never hung off
`DatabasePipe`).

Idempotency is the **dedup key**: every discovered item carries one (e.g.
`session-stats:session:{id}`), and the partial unique index on
`(pipeline, dedup_key)` spans *all* statuses - once a key's job exists
(queued, running, succeeded, or dead), re-enqueueing it is a no-op forever.
The discovery query should also exclude already-enqueued work so repeated
passes stay cheap — anti-join on indexed columns (`NOT EXISTS` over
`(pipeline, session_id)`, both sides indexed), never by reconstructing the
dedup key per row, which no index can serve; the unique index is the
correctness backstop either way. Discovered batches are enqueued in one
statement (`pipe.jobs.enqueue_many`), not a transaction per item.

Pipelines fed only by chaining (or manual enqueue) return `()`.

## Blob space and artifacts

- Each pipeline owns the blob prefix **`{name}/`** at the bucket root -
  `storage.keys.pipeline_key(name, session_id, filename)` yields
  `{name}/sessions/{session_id}/{filename}`. (`users/` is reserved by segment
  storage; the registry rejects that name.)
- Outputs are recorded in **`pipeline_artifacts`**: `(pipeline, kind,
  session_id, start_ms, end_ms, bucket, object_key, links, metadata)`.
  `kind`, `links`, and `metadata` mean whatever the pipeline says they mean;
  `object_key` is NULL for artifacts that are pure rows. Consumers locate
  upstream outputs with `pipe.artifacts.find(pipeline, kind, session_id)`
  (newest wins) or `find_overlapping(session_id, from_ms, to_ms, ...)`.
- **Artifacts address session time, not segments.** `start_ms`/`end_ms` place
  an artifact on the session's timeline (ms from session start; both NULL =
  the whole session). `services/timeline.py` is the bridge: it maps the
  uniform-WAV stitched stream to that timeline, renders any `[start_ms,
  end_ms)` as a standalone clip, and walks whole sessions in windows. The
  segment is an implementation detail of capture — labels, transcripts, and
  embeddings all attach to *chunks of a session*. The API surfaces them at
  `GET /v1/sessions/{id}/artifacts` (filters: pipeline, kind, time window).
- **A job can be scoped to the artifact it consumes** (`processing_jobs.
  artifact_id`, FK `ON DELETE SET NULL`). That is how tier N+1 discovers tier
  N's output with an indexed anti-join — see the transcription tiers below.
- Deleting a session cascades its jobs and artifact rows, but blobs under
  `{name}/sessions/{id}/` are currently orphaned - a future cleanup pass owns
  that.

## Writing a pipeline

1. Subclass `processing.base.Pipeline`; set `name` (unique; also the blob
   prefix and the `processing_jobs.pipeline` value).
2. Implement `discover(limit)` with its queries in
   `database/repos/pipelines/<name>.py`, stamping dedup keys.
3. Implement `process(job) -> dict` - query whatever you need (many
   segments, other pipelines' artifacts, nothing at all), write blobs under
   your prefix, record artifacts. The returned dict is stored as
   `jobs.result`. Raise to fail: the worker retries with backoff, then marks
   the job dead.
4. Heavy resources (models, clients) load in `setup()`, once per child
   process - **never at module import**. Module tops import only stdlib,
   `processing.base`, `database.*`, `storage.*`.
5. Register the class in `processing/registry.py:PIPELINES`, and wire a
   callback there if something should follow success.

`session-stats` and `stats-echo` (`processing/pipelines/`) are the living
templates: the first discovers ended sessions, aggregates every segment, and
writes a blob + artifact; the second is chained-only and consumes the
artifact - the transcription → post-processing shape without the
transcription.

## The transcription tiers

Real tiered processing, consuming through artifacts (no chaining callbacks —
each tier discovers its own input):

1. **`speech-detect`** (`processing/pipelines/speech_detect.py`) — discovers
   ended sessions (shared query: `database/repos/pipelines/common.py`), runs
   the session's PCM through a VAD backend (`processing/vad.py`: `silero` via
   pysilero-vad on CPU, or the deterministic `energy` threshold the tests
   use), and records one `speech-span` artifact per merged/padded span plus a
   `speech-map` summary. The threshold is deliberately low (0.15): spans are
   a **coarse, high-recall activity gate** that decides what the diarizer
   sees — they do not gate ASR. (Measured: at 0.5 silero covered 4% of a far
   speaker's talk on a glasses mic; pyannote's own segmentation covers it
   all, so precision here would silence one side of a conversation.)
   Unprocessable sessions (non-WAV, non-16k-mono, no audio) get an empty map
   with a `skipped` reason — never a dead job. Republication replaces the
   previous span set in one transaction.
2. **`diarize`** (`processing/pipelines/diarize.py`) — consumes each
   session's `speech-map` (one job per session), but the diarizer never sees
   a whole session: spans re-merge into *blocks* (contiguous speech, gap ≤
   30 s joins, ≤ 30 min, closed at span boundaries) because label consistency
   needs long context. Emits `speaker-turn` artifacts with
   **block-namespaced** labels (`b{start}:SPEAKER_00` — local identity only;
   global identity is the clustering tier's job), **`utterance`** artifacts —
   same-speaker turns merged when the gap is ≤ 1.5 s, capped at the ASR
   input window (30 s), the units the transcribe tier consumes — and one
   `speaker-embedding` artifact per (block, final label) whose vector lives
   in the `speaker_embeddings` table (pgvector, cascade-deleted with its
   artifact row), queryable with distance operators for the clustering tier.

   The service returns per-turn embeddings (overlap masked out) besides its
   per-label aggregates, and the tier runs a **purity audit** before
   publishing (`split_labels`): the diarizer sometimes puts several people
   under one label — its aggregate, a blend of their voices, can't reveal that,
   and it would corrupt a voice-print and every transcript resolved through
   it. When a label's own turn vectors form ≥2 well-separated groups
   (`diarize_split_distance`, looser than the corpus threshold; each group ≥
   `diarize_split_min_clean_ms` of clean talk), the label splits into
   sub-labels (`b{start}:SPEAKER_10.0`, `.1`, … — numbered by descending
   clean talk) and turns, utterances, transcripts, and voice-prints all
   derive from the corrected labels. The audit errs toward splitting: a
   false split is two clusters of one person, repairable with the existing
   merge/pin tools; a false merge is unrepairable. Per-turn vectors are
   deliberately ephemeral — republication recomputes them, and INFO logs
   carry the sub-cluster distances for threshold calibration.

   **Overlap is metadata, never a gate.** Turns and utterances carry
   `overlap_ms` (time shared with other speakers; utterances prorate their
   turns'), embeddings carry `clean_talk_ms`, and split prints carry
   `split_of` — but overlapped speech is always still diarized and
   transcribed; crosstalk is queryable, not suppressive. Voice-prints are
   the clean-talk-weighted mean of the label's turn vectors (crosstalk
   frames excluded; service aggregate as fallback when nothing is
   poolable — exactly the print the clean-talk gate then skips). Utterance
   merges refuse to span another speaker's interjection beyond
   `diarize_merge_crosstalk_max_ms` (gated on the *raw* turn list, so
   sub-`min_turn_ms` interjections still count): ASR transcribes the whole
   rendered span, so a spanned interjection would land its words in the
   wrong transcript. A backchannel within the budget still merges — a grunt
   shouldn't split a sentence.

   Publication is atomic: delete-previous + insert-all + `diarize-map` in one
   transaction — vectors included — **and it deletes the session's
   transcripts too**: they derive from utterances that no longer exist, and
   this tier owns invalidating them. The map's presence is the completion
   marker and retries are idempotent. A malformed embedding from the service
   is dropped with a warning (turns are the load-bearing output); a service
   without per-turn support degrades to exactly the pre-audit behavior.
   Service seam: `processing/diarizer.py` → the diar_pyannote container
   (`PROCESSING_DIARIZER_BASE_URL`), BUT-FIT/diarizen-wavlm-large-s80-md-v2
   (WavLM+Conformer segmentation) embedding with WeSpeaker ResNet34-LM — the
   same 256-d embedder pyannote community-1 used, so the switch left the
   vector space and the clustering thresholds untouched.
3. **`transcribe`** (`processing/pipelines/transcribe.py`) — discovers
   `utterance` artifacts with no transcribe job (anti-join on
   `processing_jobs.artifact_id`), renders each from the timeline, sends it
   to the transcription service, and records a `transcript` artifact on the
   same range — full text, speaker label, model, and the utterance's
   `overlap_ms` in metadata (crosstalk stays queryable per transcript
   without joining back to the utterance); **no blob** (utterances are
   short, the row is the store). When diarize republishes,
   utterances get new ids, so the anti-join re-enqueues them and queued jobs
   whose utterance vanished skip themselves. Transcripts therefore wait on
   the whole session's diarization — the cost of gating ASR on the detector
   that actually hears every speaker.
4. **`speaker-cluster`** (`processing/pipelines/speaker_cluster.py`) —
   discovered per `diarize-map`, but every run **re-clusters the user's whole
   corpus** with constrained agglomerative clustering (average linkage,
   cosine, one threshold: `cluster_distance`, per-user overridable via the
   `cluster_params` table / `POST /v1/speakers/cluster-params`). Batch
   re-clustering is order-independent and self-healing — no centroid drift,
   no permanent splits. User curation persists as **pins** (`speaker_pins`:
   "this voice-print IS this identity", created by merges and member moves):
   pin-groups enter the agglomeration pre-merged, and clusters pinned to
   different identities never merge. After each run, result clusters map
   back to persistent `speakers` rows — pinned identity first, else majority
   previous membership preferring named clusters (named identities keep
   their id), else unnamed ids churn. Assignments live in
   `speaker_embeddings.speaker_id` — a resolve-at-read mapping, so
   re-clustering never rewrites transcripts; local labels stay provenance.
   Embeddings under `cluster_min_talk_ms` of speech are skipped as
   unreliable, unless pinned — the gate reads **clean** (non-overlapped)
   talk when the diarizer recorded it, falling back to raw talk for prints
   from before overlap tracking: a voice that only ever spoke under
   crosstalk is exactly the unreliable print the gate exists to skip. The
   members inspection clip is the label's *cleanest* utterance (most
   non-overlapped audio), not its longest — in a crowded scene the longest
   clip is full of other people talking over, and a listener would blame the
   voice-print for voices never assigned to it. The same batch runs on demand via
   `POST /v1/speakers/recluster`, `cli speakers recluster` (one-off
   `--distance`/`--min-talk-ms` flags) and the web UI's clustering-settings
   panel; all three serialize per user on an advisory lock. Manual tools:
   `POST /v1/speakers/{id}/merge` (pins both member sets to the survivor;
   `GET /v1/speakers/{id}/similar` ranks candidates by centroid distance),
   `GET /v1/speakers/{id}/members` (voice-prints farthest-first with
   playable utterance spans), and per-member reassign/eject/unpin routes.

The transcription service is behind a seam (`processing/transcriber.py`):
`PROCESSING_TRANSCRIBER_BASE_URL` speaking the standard transcriptions API
(`openai` protocol — our `asr_parakeet` container serving
nvidia/parakeet-tdt-0.6b-v3, or speaches, or a hosted endpoint) or a
Replicate cog wrapper (`cog`). Swapping models/backends is env-only.
Parakeet's TDT decoder emits native word timestamps and auto-detects among
25 European languages; the sidecar returns them clip-relative and the tier
stores them session-absolute in transcript metadata (`words`:
`[{w, s, e}]` ms, plus `language`) — the timeline API still serves
utterance-granularity spans, and a user edit drops `words` (the timings no
longer match the text). A text-only backend keeps working: `words` and
`language` are optional extensions of the response. Transcript full-text
search still uses the `'english'` FTS config (migration 0009) — revisit if
non-English `language` values start appearing. `cli sessions retranscribe
<id>` redoes one session's transcripts with the current backend (deletes the
tier's artifacts + job history; discovery re-queues every utterance;
manual edits are lost).

**transcribe-ab** (`processing/pipelines/transcribe_ab.py`) is the
model-evaluation tier: chained-only (its `discover` returns nothing) —
`POST /v1/sessions/{id}/ab` inserts the job directly. One run republishes,
per utterance, four `transcript-candidate` artifacts: parakeet and whisper
(the sidecar serves both; `?model=` selects), each via two strategies —
`chunk` (the utterance clip, as production does) and `block` (the whole
diarize block transcribed once, words rebased to session time and assigned
to utterances by midpoint; a crosstalk word lands in every covering span).
Candidates are served BLIND by `GET /v1/sessions/{id}/ab` (no model/strategy,
deterministic shuffle) and never touch the canonical transcript; voting
(`POST /v1/sessions/{id}/ab/votes`) derives model/strategy server-side from
the winning candidate, upserts one `ab_votes` row per utterance (model +
strategy denormalized so the tally survives regeneration), and promotes the
winner's text/words into the `transcribe`/`transcript` artifact — the one
location the timeline and search read. `GET /v1/ab/results` is the running
model × strategy tally. Frontend: the "ab" tab (tally + enroll) and the
per-session blinded voting page (A–D rows, keyboard 1–4/j/k/space, reveal
after vote). **The first experiment concluded 2026-08 — parakeet·chunk won;
see `docs/asr-ab-results.md`.** Whisper is disabled in the sidecar until
re-enabled (`ASR_WHISPER_ENABLED=1`); enrolling sessions meanwhile yields
parakeet-only candidates (whisper calls count as errors).

## The audio-tag tiers

`audio-tag` (`processing/pipelines/audio_tag.py`) runs beside the speech
tiers, not downstream of them: it discovers ended sessions directly (same
trigger as `speech-detect`) because non-speech sound — traffic, music, a
keyboard — is exactly what it exists to hear. `sound-span` then runs
downstream of it, turning its windows into something readable.

1. **audio-tag** — walks the session's audio in ~2-minute rendered spans
   (overlapping by window-minus-hop so the service's 10 s / 5 s-hop window
   grid stays continuous across seams) and sends each span to the
   classification service behind `processing/classifier.py`
   (`PROCESSING_CLASSIFIER_BASE_URL` — our `audio_tagger` container serving
   CED-base for AudioSet's 527 sound classes plus LAION-CLAP for audio
   embeddings; `POST {base}/audio/analyze`). Publishes per ~10 s window one
   `audio-tag` artifact — top tag scores in metadata, ontology ancestors
   suppressed (AudioSet is a DAG: a guitar clip scores Music, Musical
   instrument *and* Guitar; the window keeps the most specific winner) — and
   one `audio_embeddings` pgvector row keyed by that artifact. The
   `audio-tag-map` written last carries the session-level tag list: per-class
   MAX across windows (events are sparse; a mean buries them), gated on
   holding `audio_tag_threshold` for `audio_tag_min_consecutive` windows in a
   row. The ontology parent map is vendored data
   (`processing/data/audioset_parents.json`, generated from
   github.com/audioset/ontology).

2. **sound-span** (`processing/pipelines/sound_span.py`) — consumes those
   windows and publishes the readable shape: few long `sound-span` artifacts
   carrying **one class each**, plus a `sound-span-map` marker. Spans of
   different classes overlap freely (music under speech under typing); spans
   of one class never do, which is what lets a class render as a single
   timeline lane. It triggers on `audio-tag-map` exactly as `speaker-cluster`
   triggers on `diarize-map`, and reads the windows through
   `database/repos/pipelines/sound_span.py`.

   Spans are built with **hysteresis**, not one threshold: the tagger's
   per-window sigmoids wobble by hundredths, so a class near a single cut-off
   shatters into fragments. A span opens at `sound_span_enter_score` and
   extends while `sound_span_sustain_score` holds; reopening after a close
   costs `enter` again. `sustain` must stay above `audio_tag_window_min_score`
   — below that floor a label is simply absent from a window's metadata, so a
   lower sustain would hand the decision to the service's floor. Dropouts are
   bridged by `sound_span_bridge_gap_ms`, measured as *milliseconds of audio
   no member window covered* — grid-free on purpose, so it survives missing
   windows, audio-tag's seam dedupe, and hop changes. The evidence floor is
   `sound_span_min_windows` rather than a duration, because a one-window span
   is already a window wide. Classes rank by total covered time and are kept
   whole (`sound_span_top_k`); `sound_span_max_spans` is the row guard against
   `create_many`'s bind-parameter ceiling.

   **A span edge is not an event onset.** Edges are the union of their member
   windows, so they are smeared by up to one window either way — the tagger
   offers no sub-window localization. A later tier could sharpen them by
   re-rendering the seconds around each edge at a fine hop without changing
   anything here.

   Invalidation is the standard artifact story: audio-tag republishing deletes
   the old map, which nulls `artifact_id` on any queued sound-span job (it
   skips), while the new map mints a fresh job that replaces the tier's whole
   output. Audio-tag needs no reciprocal delete — unlike diarize→transcribe,
   this is one job per session that already clears its own output first. Stale
   spans therefore survive for the seconds between the two, and indefinitely
   if the job dead-letters: the same eventual consistency `speaker-cluster`
   lives under.

The embeddings are the retrieval half of question-answering over recordings:
`GET /v1/search/audio?q=…` embeds the text through the same service's CLAP
text encoder and ranks the caller's windows by cosine distance — consumers
then feed the hit windows' transcripts and tags to an LLM. The vectors are
never fed to a model directly.

Search is a trio: contrastive audio (`/search/audio`, "when did I hear X"),
tag filtering (`/search/tags`, calibrated class scores), and lexical
transcript search (`/search/transcripts`, "when was X said" — Postgres FTS
with a trigram fuzzy fallback; indexes in migration 0009, queries in
`database/repos/transcripts.py`, no pipeline involvement). Semantic
transcript search (sentence embeddings + a `transcript-embed` tier) is
deliberately deferred pending a chunk-overlap design; it would arrive as a
non-breaking `mode=` parameter.

## Callbacks and chaining

`Pipeline.__init__` takes an optional callback; `maybe_callback(job, result)`
invokes it **after the success is committed**. Semantics: at-most-once and
best-effort - a callback exception is logged and swallowed (the job is
already `succeeded`), and a crash between commit and callback drops the
invocation. The registry's `enqueue_follow_up(target)` factory is the
standard chaining callback: it enqueues `target` with the parent's session
linkage, inherited priority, and payload
`{"source_job_id", "source_result"}`, deduped per parent job. If a chain ever
needs to be durable rather than best-effort, enqueue the follow-up inside the
succeed transaction instead - nothing in the schema prevents that.

## Operations

- `make worker`, or `uv run python -m processing --only session-stats`.
- `PROCESSING_*` environment (see `.env.example`): poll interval, discovery
  batch, retry policy, shutdown grace, restart backoff.
- SIGTERM/SIGINT: children finish the in-flight job, the supervisor waits
  `PROCESSING_SHUTDOWN_GRACE_SECONDS`, then kills stragglers. A SIGKILLed
  child leaves a `running` row; the next boot requeues it.
- Priority: plain integer, higher first, default 0. Nothing assigns
  priorities yet; chained jobs inherit their parent's. Constant load at a
  high priority will starve lower ones - there is no aging.

## Live pipelines

Live processing (acting on a session *while* it streams) is its own kind,
built in `live/` and deliberately not part of the batch machinery above: no
jobs, no artifacts, no queue contention. It is **best-effort by contract** —
the durable path is the websocket's segment pooler; the live path may drop
frames under pressure and misses audio across a worker restart.

```
                  websocket (v2)                    live worker (child process)
 client ──frames──▶ api ──┬─ pooler ─▶ blob+rows    ┌──────────────────────────┐
                          └─ tap ── UDS per conn ──▶│ hello → SessionStream    │
                                        ◀── events ─│  └ WakewordGate          │
                                                    │     └ LivePipeline × N   │
                                                    └──────────────────────────┘
```

- The API spawns **one live worker child** at startup (`live/supervisor.py`,
  spawn context — never fork a running uvicorn — restart with the same
  backoff/reset scheme as the batch supervisor). The worker listens on a Unix
  domain socket (`LIVE_SOCKET_PATH`, default pid-suffixed under
  `/tmp/audio-pipeline/`).
- The **tap** (`live/tap.py`) opens **one UDS connection per v2 websocket
  connection**: connect = attach (a JSON `HELLO` with session, PCM params,
  and negotiated effects), close = detach, and `EVENT` messages flow back on
  the same socket into the connection's outbox as `effect` events
  (docs/streaming-protocol.md). `send_frame` never blocks the ingest path: a
  bounded per-connection queue drops its oldest under pressure, and while the
  worker is down frames simply drop as the tap reconnects with backoff.
- Worker-side (`live/worker.py`, `live/sessions.py`), each connection gets a
  `SessionStream` holding a **wakeword gate** (`live/gate.py`): idle, it
  feeds a detector (`live/detect.py` — a stub matching `LIVE_WAKEWORD`'s
  bytes in the PCM, existing only so tests can trigger deterministically) and
  a `LIVE_PREROLL_MS` ring; on a trigger it starts one instance of each
  effects-enabled pipeline and feeds pre-roll then live frames until
  `LIVE_GATE_WINDOW_MS` of audio has passed (silence-based close is a future
  detector hook), then re-arms.
- A **live pipeline** (`live/base.py`) is `async run(ctx, frames)` over an
  async iterator of PCM frames — it owns its loop, may await inference
  freely (the gate's bounded queue drops behind a slow consumer), and knows
  nothing about wakewords. `ctx.emit(event, data)` publishes an effect event.
  Register in `live/registry.py`; `live-counter` (`live/pipelines.py`) is the
  stub template. A live pipeline may share its core code with a paired batch
  pipeline (live transcription and batch transcription wrapping one
  transcriber) — that pairing is still future work, as is the
  follower-per-session pattern for live consumers that need the stores
  rather than the socket.

`LIVE_*` environment (see `.env.example`): wakeword, socket path, pre-roll
and window sizes, queue bounds, restart/shutdown tuning; `LIVE_ENABLED=false`
turns the whole layer off (v2 streaming is unaffected).
