"""API settings, loaded from the environment / .env file.

Everything here tunes the streaming websocket (docs/streaming-protocol.md);
the HTTP routes need no configuration of their own.
"""

from datetime import time
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ApiSettings(BaseSettings):
    """Streaming limits and timeouts, read from API_* variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="API_",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Must stay at or below uvicorn's websocket message limit (16 MiB default);
    # frames past that limit tear the connection down instead of erroring.
    stream_max_chunk_bytes: int = 8 * 1024 * 1024
    stream_ack_window_chunks: int = 10
    stream_ack_window_seconds: float = 2.0
    stream_hello_timeout_seconds: float = 10.0
    stream_idle_timeout_seconds: float = 300.0
    # Chunk stores (blob PUT + row insert) in flight at once per connection.
    # The protocol's cumulative acks are gap-aware, so out-of-order completion
    # is already handled; 1 restores strictly sequential storage.
    stream_ingest_concurrency: int = 4
    # How often a quiet-but-connected stream re-checks its session row, so a
    # split (or any external end) reaches the client promptly instead of on
    # its next chunk. Keep well under session_stale_seconds: a rotated client
    # must reconnect long before its successor session could be swept.
    stream_session_check_seconds: float = 5.0

    # Protocol v2: per-frame PCM cap and the pooling of frames into stored
    # segments (api/stream_pool.py). The ack `through` for a frame waits on
    # its pooled segment, so max_latency is also the worst-case ack lag.
    stream_max_audio_frame_bytes: int = 64 * 1024
    stream_pool_target_bytes: int = 256 * 1024
    # Hard per-connection cap on buffered-but-not-durable PCM; past it the
    # pump stops reading and backpressure lands in TCP.
    stream_pool_max_buffer_bytes: int = 1024 * 1024
    stream_pool_max_latency_seconds: float = 10.0
    stream_pool_flush_retries: int = 3
    stream_pool_retry_backoff_seconds: float = 1.0

    # An open session with no activity for this long is ended by the sweeper.
    # Keep it at or above the idle timeout, or a quiet-but-connected client's
    # session can be swept out from under it. Chunk heartbeats are throttled to
    # roughly one write a minute (sessions.touch_if_stale), so keep this well
    # above that interval too.
    session_stale_seconds: float = 300.0
    session_sweep_interval_seconds: float = 60.0

    # Split every open session at this local wall-clock time ("HH:MM") each
    # day, so long-running captures close and enter processing on a schedule.
    # Unset disables. The container's TZ decides what "local" means.
    session_rotate_at: str | None = None

    @field_validator("session_rotate_at")
    @classmethod
    def _valid_rotate_at(cls, value: str | None) -> str | None:
        if value is not None:
            time.fromisoformat(value)  # bad config fails at boot, not at 03:30
        return value

    # Text->audio search embeds its query via the classifier sidecar (the
    # audio_tagger container, same default the worker uses). A text encode is
    # fast, but with IDLE_UNLOAD_SECONDS set the sidecar may first have to
    # reload CLAP from disk — the timeout must cover that, not just a
    # contended GPU.
    classifier_base_url: str = "http://127.0.0.1:8035/v1"
    classifier_timeout_seconds: float = 180.0


@lru_cache(maxsize=1)
def get_settings() -> ApiSettings:
    return ApiSettings()
