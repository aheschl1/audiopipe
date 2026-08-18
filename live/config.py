"""Live-layer settings, loaded from the environment / .env file."""

import os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class LiveSettings(BaseSettings):
    """Wakeword, socket, and queue tuning, read from LIVE_* variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="LIVE_",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    enabled: bool = True
    # Empty selects a pid-suffixed default, so each API process (and its own
    # worker child) gets a private socket.
    socket_path: str = ""
    wakeword: str = "hey pipeline"

    # Audio handed to pipelines from just before the trigger, and how much
    # audio a gated window spans before the pipelines are finished.
    preroll_ms: int = 2000
    gate_window_ms: int = 30_000

    # Bounded frame queues, both drop-oldest: the tap's per-connection send
    # queue and the per-pipeline feed inside the worker.
    tap_queue_frames: int = 256
    pipeline_queue_frames: int = 256

    reconnect_backoff_seconds: float = 0.5
    reconnect_backoff_cap_seconds: float = 5.0

    # Supervisor: SIGTERM -> SIGKILL window and child-restart backoff.
    shutdown_grace_seconds: float = 5.0
    restart_backoff_base_seconds: float = 1.0
    restart_backoff_cap_seconds: float = 30.0
    restart_reset_seconds: float = 60.0


def socket_path(settings: "LiveSettings") -> str:
    """The effective UDS path: configured, or the pid-suffixed default."""
    if settings.socket_path:
        return settings.socket_path
    return f"/tmp/audio-pipeline/live-{os.getpid()}.sock"


@lru_cache(maxsize=1)
def get_settings() -> LiveSettings:
    return LiveSettings()
