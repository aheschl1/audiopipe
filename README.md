# context

A self-hosted context layer: phones and glasses stream audio, location, photos and
video into sessions; background pipelines transcribe, diarize, tag and derive media;
agents and the web app read it back as one timeline.

## Database layer

Postgres accessed through raw SQL repositories behind a single entry point:

```python
from database import DatabasePipe
from database.schema.users import UserCreate

async with DatabasePipe() as pipe:
    user = await pipe.users.create(UserCreate(email="me@example.com", password="s3cret"))
    issued = await pipe.tokens.issue(user.id, name="laptop", ttl=timedelta(days=30))
    print(issued.token.get_secret_value())  # shown once; only the hash is stored
```

Everything inside one `async with` block runs on one connection in one transaction:
it commits on a clean exit and rolls back if an exception escapes. Repositories return
Pydantic models from `database/schema/`.

| Path | Purpose |
| --- | --- |
| `database/pipe.py` | `DatabasePipe`, connection pool (`get_pool` / `close_pool`) |
| `database/repos/` | Raw-SQL repositories (`pipe.users`, `pipe.tokens`, `pipe.sessions`, `pipe.segments`) |
| `database/schema/` | Pydantic models returned by the repositories |
| `database/security.py` | argon2 password hashing, token generation/digest |
| `database/migrations/` | Alembic revisions (hand-written SQL, no autogenerate) |
| `storage/` | Blob store: `BlobPipe`, the `BlobStore` seam, S3 implementation, key layout |
| `services/audio.py` | Ingest and deletion — the operations spanning both stores |
| `api/` | FastAPI service: `main.py`, `deps.py`, `routes/`, `schema/` |
| `cli/main.py` | Admin CLI |

## Audio storage

Audio is grouped as `recording_sessions` → `audio_segments`. A segment row is
metadata plus a pointer (`bucket` + `object_key`); the bytes live in the blob
store, never in Postgres.

```python
from services import audio

segment = await audio.ingest_segment(session_id, data, content_type="audio/wav")
url = await audio.segment_url(segment.id)
```

`ingest_segment` writes the object first and then the row, deleting the object
if the row fails — so a failed ingest leaves nothing behind. Deletion goes the
other way (row, then object). Use `services.audio.delete_segment` /
`delete_session` rather than the repositories directly: Postgres cascades remove
rows but cannot touch blobs, so deleting a user through `pipe.users.delete()`
orphans their objects.

### Blob store

The bytes live in a **MinIO** service, or any other s3 compatible blob storage.

Object keys are plain paths inside the bucket
(`users/<uuid>/sessions/<uuid>/…`) with no service-name prefix.

## HTTP API

```bash
uv run python -m api
```

Authenticate with a token from `tokens issue`:

```bash
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/v1/sessions
```

Lists take `limit` (<=200) and `offset`, and return `{items, limit, offset,
has_more}`. `.

### Audio delivery

`GET /v1/segments/{id}/audio` is an HTTP media endpoint, not a bare
download:

- **`Range` requests** (RFC 9110) return `206 Partial Content` with
  `Content-Range`. This is what lets a browser's `<audio>` element seek and an
  interrupted transfer resume. The range is passed through to the object store,
  so only the requested slice crosses the network. Multi-range requests and
  malformed `Range` headers fall back to the whole object, as the spec allows;
  a well-formed but out-of-bounds range gets `416`.
- **`ETag` + `If-None-Match`** → `304`. The tag is the segment's stored SHA-256,
  which is a genuinely strong validator, and segments are immutable once
  written, hence `Cache-Control: private, max-age=31536000, immutable`.
- **`If-Range`** serves the full object when the client's copy is stale, so a
  fresh range can never be stitched onto older bytes.

Longer term, playing a whole session as one continuous track is what HLS
(`.m3u8` playlist per session) is for — `recording_sessions → audio_segments` is
already shaped like it. That needs `duration_ms` populated and a settled codec,
so it is not implemented yet.

### Setup

1. Copy `.env.example` to `.env` and fill in the `DATABASE_*` and `STORAGE_*`
   credentials. `STORAGE_ACCESS_KEY`/`STORAGE_SECRET_KEY` are the MinIO
   credentials, so they must match whatever store you point at.
2. `uv sync`
3. Start Postgres and MinIO, unless you already run them:
   `docker compose -f deploy/docker-compose.yml --profile stores up -d postgres minio`
   — both publish on loopback at the default ports.
4. `make sidecars` — the three model containers (`sidecars/`), on
   `127.0.0.1:8033-8035`, which is where the code's defaults already point.
5. `uv run alembic upgrade head`, then `uv run python -m cli.main blobs check`

### CLI

```
uv run python -m cli.main check
```

`pyproject.toml` declares the `context` entry point, but with no build backend
the project is not installed onto `PATH` — use `python -m cli.main`.

## Web frontend (`frontend/`)

A React + TypeScript browser UI over the API: session browser (with in-place
session renaming), timeline (transcripts, speakers, sound tags), seekable audio
playback, text→audio search, and speaker curation: labeling, merging split voices (distance-ranked
candidates, same-name prompt), inspecting a cluster's voice-prints by ear,
moving/ejecting wrong voices (pinned so rebuilds honor it), and tuning the
clustering threshold + recomputing from a settings panel. The "map" tab plots
the voice-prints themselves — a PCA of the corpus the clusterer clusters, so
separation can be seen rather than inferred from distances
(`docs/speaker-map.md`). In development it runs against the Vite dev server;
deployed, the same bundle is served by the stack's `web` container. The "ab" tab runs
the transcription A/B: blinded per-utterance voting between parakeet and
whisper (chunk vs whole-block strategies) — the winner becomes the timeline
transcript and feeds a model × strategy tally. Locally:

```bash
make api    # terminal 1 — the API on 127.0.0.1:8000
make web    # terminal 2 — Vite dev server; proxies /v1 + /health to the API
```

Log in with an account from `cli users create`. Models are **generated, never
hand-written**: `make gen-client` regenerates `frontend/openapi.json` (a pure
function of the route/model declarations — no server needed) and
`frontend/src/api/schema.d.ts` via openapi-typescript; requests go through a
typed openapi-fetch client. Regenerate after any API change — `npm run build`
(tsc) fails on drift. Audio playback uses a minutes-lived token minted by
`POST /v1/sessions/{id}/playback` in the audio URL's `?token=`, because media
elements cannot send an Authorization header.

Pages are URLs (react-router; nginx and Vite fall back to `index.html`), so a
reload or a shared link lands on the same screen: `/sessions`, `/sessions/<id>?t=<ms>`,
`/search?mode=describe|classes|transcripts&q=…`, `/speakers?speaker=<id>&tab=transcripts|prints`,
`/speakers?view=map&proj=12|13|23|3d`, `/map`, `/ab`, `/ab/<id>`. Defaults are
omitted from the query string. Top-level route names must not start with the
prefixes nginx proxies to the API (`v1`, `health`, `docs`, `redoc`, …).

## Model sidecars (`sidecars/`)

Three GPU serving containers, each a Dockerfile plus one `app.py`. They are the
tiers the worker cannot do in-process:

| Directory | Container | Port | Serves |
| --- | --- | --- | --- |
| `asr_parakeet` | `asr-parakeet` | 8033 | parakeet-tdt transcription (+ a whisper A/B slot) |
| `diar_pyannote` | `diar-pyannote` | 8034 | pyannote diarization, per-turn embeddings |
| `audio_tagger` | `audio-tagger` | 8035 | CED sound tags + CLAP embeddings |

Each has its own README covering models, API and knobs. `diar-pyannote` needs
an `HF_TOKEN` in `deploy/.env` — its weights are gated.

## Deployment (`deploy/`)

`deploy/docker-compose.yml` runs the whole thing: `migrate`, `api`, `worker`, a
`web` tier (nginx serving the built SPA and relaying the API on the same
origin), and the three sidecars.

```bash
cp deploy/.env.example deploy/.env    # credentials; addresses have defaults
docker compose -f deploy/docker-compose.yml --profile stores up -d
```

That is a complete deployment on a fresh machine: the `stores` profile adds a
pgvector Postgres and a MinIO, and the app defaults to them. To use stores you
already run instead, set `DATABASE_HOST` and `STORAGE_ENDPOINT_URL` in
`deploy/.env` and leave the profile off — which is what the Makefile does:

```bash
make stack-up                         # build and start; no stores profile
make stack-ps                         # migrate exited 0, api healthy, worker up
make stack-logs S=worker
```

Only the `web` tier publishes a port (`WEB_BIND`/`WEB_PORT`, default
`127.0.0.1:8098`), and it speaks plain HTTP — put a TLS-terminating proxy in
front of it. That proxy needs a long read timeout, since the ingest websocket
tolerates 300s of silence, and buffering off, since session audio is a
Range-able stream of arbitrary length. `deploy/web/nginx.conf` sets both for
the inner hop and is the model to copy.

**Sidecar addresses switch on their own.** Every setting is pydantic-settings,
where a real environment variable outranks the dotenv file, so compose points
the containers at `http://asr-parakeet:8000/v1` and friends while the code's
defaults stay `http://127.0.0.1:8033/v1`. The sidecars keep publishing those
loopback ports either way, so `make sidecars` + a host-run `make api` /
`make worker` needs no configuration and no separate profile. There is no
branch anywhere in the code for this.

Two constraints the compose file encodes: the worker is never scaled (one
supervisor, one child per pipeline — `docs/processing-pipelines.md`), and it
waits for the sidecars to report healthy, because a job that meets a cold
sidecar burns all five attempts inside 30s while weights are still loading.

## Tests

```bash
uv run pytest              # everything
uv run pytest tests/unit   # pure logic, no Docker
```

`tests/e2e/` starts throwaway Postgres and MinIO containers (testcontainers),
applies the migrations, runs the API as a real `python -m api` process on a
loopback port, and drives it with `httpx`.
