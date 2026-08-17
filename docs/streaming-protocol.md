# Streaming upload protocol

Version 1. Server frame models live in `api/schema/stream.py`; the handler in
`api/routes/stream.py`. The server publishes the frame schema as JSON Schema
at **`GET /stream-schema.json`** (next to `/openapi.json`) — clients generate
their frame types from that document rather than transcribing this file.

Clients record short, self-contained audio chunks and upload each one as a
single websocket message. The server stores every chunk as one `AudioSegment`
(visible immediately through the REST API) and answers with typed JSON events:
cumulative acknowledgements today, effect output (transcription, VAD, …)
tomorrow, over the same connection.

## Connecting

```
WS /v1/sessions/{session_id}/stream
Authorization: Bearer <token>
```

The session is created first over REST (`POST /v1/sessions`) and must be open
and owned by the caller. The bearer token is the same one REST uses, and there
are two ways to present it:

**Header mode (canonical).** Send `Authorization: Bearer <token>` on the
upgrade request. The server authenticates before accepting, so a failed
handshake is a rejected upgrade with a real HTTP status and the REST error
body (`{"detail": ...}`):

| status | meaning |
|---|---|
| 401 | missing or invalid bearer token |
| 404 | session does not exist, or belongs to someone else |
| 409 | session has already ended |

**Hello-token mode (fallback).** Clients whose websocket cannot set request
headers — browsers, embedded runtimes — omit the header and put the token in
the `hello` frame (`hello.token`). The server accepts the socket first, then
authenticates when `hello` arrives; the same failures become close codes
instead of HTTP statuses:

| close code | meaning |
|---|---|
| 1008 | missing or invalid token |
| 4404 | session does not exist, or belongs to someone else |
| 4409 | session has already ended |

When a valid `Authorization` header is present, `hello.token` is ignored.

## Connection lifecycle

```
AWAITING_HELLO -> STREAMING -> DRAINING -> CLOSED
```

1. **AWAITING_HELLO** — after the upgrade, the client must send `hello` as its
   first frame within 10 s, or the server closes with 4400.
2. **STREAMING** — the server replies `welcome`; the client sends chunks and
   eventually `finish`.
3. **DRAINING** — on `finish`, the server flushes a final `ack`, ends the
   session, sends `bye`, and closes with 1000.

Every message is a JSON text frame with a `type` field, except the binary
`chunk` frame. **Clients MUST ignore server events whose `type` they do not
recognise** — that is how new effect events arrive without a version bump.

## Client → server

### `hello` (text)

```json
{
  "type": "hello",
  "version": 1,
  "token": null,
  "defaults": {
    "content_type": "audio/wav",
    "codec": "pcm_s16le",
    "sample_rate_hz": 16000,
    "channels": 1
  },
  "effects": []
}
```

`defaults` (all optional) apply to every **audio** chunk — they are PCM
parameters, and chunks of other resources ignore them, resolving content type
from their own header or their resource type's default. An audio chunk may
override `content_type` only. `effects` is reserved for requesting
server-side effects; no effects exist yet, and the server echoes the enabled
set in `welcome`. `token` is used only in hello-token mode (see *Connecting*).
A `version` the server does not speak closes the socket with 4400.

### `chunk` (binary)

```
+----------------------+------------------+------------------+
| header length (u32,  | ChunkHeader JSON | resource payload |
| big-endian, 4 bytes) | (header length   | (rest of message)|
|                      |  bytes of UTF-8) |                  |
+----------------------+------------------+------------------+
```

One message per chunk — atomic by construction, nothing to pair across frames.
The payload must be a complete, independently decodable unit of its resource
and must not be empty. For audio that means a whole short WAV/Opus/WebM file,
not a slice of a longer bitstream; for location, one JSON batch of points
(below).

`ChunkHeader` fields:

| field | required | meaning |
|---|---|---|
| `sequence` | yes | client-assigned, ≥ 0; consecutive from `welcome.next_sequence` |
| `resource` | no | resource type of the payload; default `audio`. The server lists what it accepts in `welcome.resources` |
| `captured_at` | no | ISO 8601 capture timestamp |
| `duration_ms` | no | payload duration/span |
| `content_type` | no | overrides the hello default (audio) or the resource default |
| `checksum_sha256` | no | hex; the server verifies and rejects a mismatch |
| `metadata` | no | JSON object, stored on the segment |

Sequences share one per-session space across resources: interleaving location
chunks between audio chunks consumes sequence numbers from the same counter,
and acks stay cumulative over that single space.

#### `location` payloads

Content type `application/json`:

```json
{"points": [{"lat": 51.04732, "lon": -114.05829, "t": 1755205000123,
             "alt_m": 1045.0, "accuracy_m": 8.5}]}
```

`t` is epoch **milliseconds**; points must be ordered by `t` (1–10 000 per
batch); `alt_m`/`accuracy_m` are optional. The stored row spans its batch:
`captured_at` defaults to the first point's time and `duration_ms` to the
first-to-last span. On the session timeline a point's position is derived
from wall clock (`t` minus the session start), which can drift from the
audio-position time other events use when capture had gaps — the same caveat
as `session-end`.

### `finish` (text)

```json
{"type": "finish", "ended_at": null}
```

Ends the session (`ended_at` defaults to now), triggers the final `ack` and
`bye`, and closes with 1000. Closing the socket without `finish` leaves the
session open for a resumed connection — see *Disconnects* below.

## Server → client

### `welcome`

```json
{
  "type": "welcome",
  "version": 1,
  "session_id": "…",
  "next_sequence": 0,
  "ack_window": {"chunks": 10, "seconds": 2.0},
  "limits": {"max_chunk_bytes": 8388608},
  "effects": [],
  "resources": ["audio", "location"]
}
```

`next_sequence` is one past the highest sequence already stored — 0 for a
fresh session, the resume point after a reconnect. `resources` lists the
resource types this server accepts in chunk headers — feature detection for
clients that stream more than audio.

### `ack`

```json
{"type": "ack", "through": 24, "count": 10, "bytes": 163840, "duplicates": [20]}
```

Cumulative: **every sequence ≤ `through` is durably stored** (bytes in the
object store, row committed). Sent when `ack_window.chunks` chunks have been
stored since the last ack, when `ack_window.seconds` have passed since the
oldest unacknowledged stored chunk, or on drain — whichever comes first.
`count`/`bytes` cover newly stored chunks since the previous ack; `duplicates`
lists retransmitted sequences that were already stored (also durable). A
`through` of -1 means nothing is stored yet.

### `error`

```json
{"type": "error", "scope": "chunk", "code": "storage_failure", "detail": "…", "sequence": 7}
```

- `scope: "chunk"` — recoverable. That chunk was **not** stored; the client may
  retransmit it at any time, including out of order. `through` will not pass a
  failed sequence until a retransmit lands.
- `scope: "session"` — fatal; followed by a close with the matching code.

| code | scope | meaning |
|---|---|---|
| `bad_header` | chunk | malformed binary envelope, header JSON, empty payload, or unknown `resource` |
| `chunk_too_large` | chunk | payload exceeds `limits.max_chunk_bytes` |
| `checksum_mismatch` | chunk | payload does not match `checksum_sha256` |
| `invalid_payload` | chunk | the payload violates its resource's contract (e.g. a malformed location batch) |
| `storage_failure` | chunk | store write failed; retransmit |
| `session_ended` | session | session was ended (REST, sweeper, or a split) mid-stream |
| `protocol_error` | session | unparseable text frame, or a second `hello` |
| `internal` | session | unexpected server failure |

### `rotate`

```json
{"type": "rotate", "through": 24}
```

The session was **split** on purpose — by the dashboard or the daily rotation
schedule — so its audio can enter processing while recording continues. The
client should create a fresh session (`POST /v1/sessions`), reconnect to its
stream, and keep recording. Chunks above `through` were not stored in the old
session; retransmit them into the successor, using the new session's sequence
space (`welcome.next_sequence`) — `captured_at` in each chunk header is what
preserves the true timeline across the boundary.

`rotate` is always followed by the `session_ended` error and a 4409 close, so
a client that predates this event ignores it and lands on the ended-session
handling it already has. Only a split emits `rotate`; an explicit REST end or
a sweeper end stays a bare `session_ended`, because a deliberate stop must
not make the device immediately re-record.

### `bye`

```json
{"type": "bye", "reason": "finished", "through": 24}
```

The last event before a server-initiated close. `reason` is `finished`
(after `finish`), `idle` (no traffic for the idle timeout; session stays
open), or `shutdown` (server going away; reconnect and resume).

## Close codes

| code | meaning |
|---|---|
| 1000 | normal close (after `bye`) |
| 1001 | server going away (drain attempted) |
| 1008 | invalid token in hello-token mode |
| 4400 | protocol error: hello missing/late/invalid, malformed text frame |
| 4404 | session missing / not owned, hello-token mode |
| 4409 | session ended (at the hello-token handshake, or mid-stream) |
| 4500 | internal error |

## Disconnects, resume, and automatic session close

An abrupt disconnect (crash, network loss, idle close) does **not** end the
session. To resume, the client reconnects to the same endpoint and retransmits
every chunk above the last `ack.through` it saw. Retransmits of chunks that
did land are deduplicated by the server's `(session, sequence)` uniqueness and
reported in `ack.duplicates` — the database is the source of truth, no
connection state survives on the server.

Sessions do not stay open forever: activity (attach, stored chunks — the
chunk heartbeat is throttled to roughly one write a minute) keeps the session
fresh, and a server-side sweeper automatically ends any open session with no
activity for `API_SESSION_STALE_SECONDS` (default 300 s).
Reconnecting after that is a 409, and a new session must be created. The
sweeper is also why a well-behaved client sends `finish`: it closes the
session at the true end of recording rather than a sweep interval later.

A session can also be ended out from under a live stream — by REST, the
sweeper, or a split (`POST /v1/sessions/{id}/split`, or the daily
`API_SESSION_ROTATE_AT` schedule). A connected stream notices within
`API_STREAM_SESSION_CHECK_SECONDS` even when quiet: a split delivers
`rotate` → `session_ended` → close 4409, any other end just
`session_ended` → 4409.

## Limits and configuration

All server-side, env-configurable (`api/config.py`), advertised in `welcome`
where the client needs them:

| variable | default | |
|---|---|---|
| `API_STREAM_MAX_CHUNK_BYTES` | 8388608 | per-chunk payload cap; stay well under uvicorn's 16 MiB frame limit |
| `API_STREAM_ACK_WINDOW_CHUNKS` | 10 | ack every N stored chunks |
| `API_STREAM_ACK_WINDOW_SECONDS` | 2.0 | …or T seconds after the oldest unacked chunk |
| `API_STREAM_HELLO_TIMEOUT_SECONDS` | 10 | AWAITING_HELLO deadline |
| `API_STREAM_IDLE_TIMEOUT_SECONDS` | 300 | close idle sockets (`bye reason=idle`) |
| `API_STREAM_INGEST_CONCURRENCY` | 4 | chunk stores in flight at once per connection |
| `API_STREAM_SESSION_CHECK_SECONDS` | 5.0 | how often a quiet stream re-checks its session for an external end/split |
| `API_SESSION_STALE_SECONDS` | 300 | auto-end sessions with no activity |
| `API_SESSION_SWEEP_INTERVAL_SECONDS` | 60 | sweeper cadence |
| `API_SESSION_ROTATE_AT` | unset | split every open session at this local time (`HH:MM`) daily |

## Example session

```
C  (upgrade with Authorization: Bearer …)
C→ {"type": "hello", "version": 1, "defaults": {"content_type": "audio/wav"}}
S→ {"type": "welcome", "version": 1, "session_id": "…", "next_sequence": 0,
    "ack_window": {"chunks": 10, "seconds": 2.0},
    "limits": {"max_chunk_bytes": 8388608}, "effects": []}
C→ [chunk seq=0] [chunk seq=1] … [chunk seq=9]
S→ {"type": "ack", "through": 9, "count": 10, "bytes": 64420}
C→ [chunk seq=10] [chunk seq=11]
S→ {"type": "ack", "through": 11, "count": 2, "bytes": 12884}     (timer fired)
C→ {"type": "finish"}
S→ {"type": "bye", "reason": "finished", "through": 11}
S  close 1000
```

## Extending with effects

Effects are server-side consumers of the chunk stream that publish their own
event types (`transcript`, `vad`, …) onto the same socket. The contract that
keeps them compatible:

- new event types appear without a `version` bump; old clients ignore them;
- effect events may reference `sequence` values or ranges, and may arrive
  *after* the `ack` covering those chunks;
- `hello.effects` requests effects by name, `welcome.effects` confirms the
  enabled set (both empty today).

Server-side, an effect is a task that consumes stored chunks and publishes to
the connection's outbox (`api/routes/stream.py`), which already serializes all
writers onto the socket.
