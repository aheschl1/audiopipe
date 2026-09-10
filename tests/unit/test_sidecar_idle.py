"""The idle-unload state machine shared by the sidecars.

The heavy imports in the sidecar apps are function-local, so the modules
import with just fastapi/numpy. The audio_tagger copy is exercised as the
representative; the other two differ only in which globals hold the models.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def tagger():
    spec = importlib.util.spec_from_file_location(
        "tagger_app_under_test", REPO_ROOT / "sidecars" / "audio_tagger" / "app.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        del sys.modules[spec.name]


def test_boot_loading_raises_instead_of_reloading(tagger) -> None:
    assert tagger._state == "loading"
    with pytest.raises(tagger.ModelsUnavailable):
        tagger._ensure_loaded()


def test_loaded_state_returns_models_and_bumps_last_used(tagger, monkeypatch) -> None:
    sentinel = ("fe", "tagger", {}, "proc", "clap")
    monkeypatch.setattr(tagger, "_models", sentinel)
    monkeypatch.setattr(tagger, "_state", "loaded")
    before = tagger._last_used
    assert tagger._ensure_loaded() is sentinel
    assert tagger._last_used >= before


def test_idle_state_reloads_through_the_loader(tagger, monkeypatch) -> None:
    sentinel = ("fe", "tagger", {}, "proc", "clap")

    def fake_load() -> None:
        tagger._models = sentinel
        tagger._state = "loaded"

    monkeypatch.setattr(tagger, "_load_models", fake_load)
    monkeypatch.setattr(tagger, "_state", "idle")
    monkeypatch.setattr(tagger, "_models", None)
    assert tagger._ensure_loaded() is sentinel
    assert tagger._state == "loaded"


def _failing_loader(tagger, monkeypatch, error="boom"):
    """A loader that always fails, wired in with the failure bookkeeping."""

    def failing_load() -> None:
        tagger._state = "failed"
        tagger._load_error = error
        tagger._record_load_result()

    monkeypatch.setattr(tagger, "_load_models", failing_load)
    return failing_load


def test_failed_reload_is_reported_as_failed_not_idle(tagger, monkeypatch) -> None:
    # Staying "idle" would keep /health at 200 while every request 503s.
    monkeypatch.setattr(tagger, "_ever_loaded", True)
    _failing_loader(tagger, monkeypatch)
    monkeypatch.setattr(tagger, "_state", "idle")
    monkeypatch.setattr(tagger, "_models", None)
    with pytest.raises(tagger.ModelsUnavailable, match="boom"):
        tagger._ensure_loaded()
    assert tagger._state == "failed"
    assert tagger.health().status_code == 503


def test_backoff_suppresses_a_second_attempt(tagger, monkeypatch) -> None:
    monkeypatch.setattr(tagger, "_ever_loaded", True)
    _failing_loader(tagger, monkeypatch)
    monkeypatch.setattr(tagger, "_state", "idle")
    monkeypatch.setattr(tagger, "_models", None)
    with pytest.raises(tagger.ModelsUnavailable):
        tagger._ensure_loaded()

    attempts = []
    monkeypatch.setattr(tagger, "_load_models", lambda: attempts.append(1))
    with pytest.raises(tagger.ModelsUnavailable):
        tagger._ensure_loaded()
    assert attempts == []


def test_exits_once_the_retry_limit_is_reached(tagger, monkeypatch) -> None:
    deaths = []
    monkeypatch.setattr(tagger, "_die", lambda reason: deaths.append(reason))
    monkeypatch.setattr(tagger, "_ever_loaded", True)
    _failing_loader(tagger, monkeypatch)
    monkeypatch.setattr(tagger, "_state", "idle")
    monkeypatch.setattr(tagger, "_models", None)
    for _ in range(tagger.RELOAD_RETRY_LIMIT):
        monkeypatch.setattr(tagger, "_retry_after", 0.0)
        with pytest.raises(tagger.ModelsUnavailable):
            tagger._ensure_loaded()
    assert len(deaths) == 1


def test_cold_start_failure_never_exits(tagger, monkeypatch) -> None:
    # _ever_loaded is False: a restart would fail the same way, so keep trying.
    deaths = []
    monkeypatch.setattr(tagger, "_die", lambda reason: deaths.append(reason))
    _failing_loader(tagger, monkeypatch, error="no cuda")
    tagger._load_models()  # the boot load, which never succeeded
    for _ in range(tagger.RELOAD_RETRY_LIMIT + 2):
        monkeypatch.setattr(tagger, "_retry_after", 0.0)
        with pytest.raises(tagger.ModelsUnavailable, match="no cuda"):
            tagger._ensure_loaded()
    assert tagger._state == "failed"
    assert deaths == []


def test_exit_mode_watchdog_exits_instead_of_unloading(tagger, monkeypatch) -> None:
    exits = []

    def fake_exit(code):
        exits.append(code)
        raise SystemExit(code)  # os._exit never returns

    monkeypatch.setattr(tagger, "IDLE_UNLOAD_EXIT", True)
    monkeypatch.setattr(tagger, "IDLE_UNLOAD_SECONDS", 1)
    monkeypatch.setattr(tagger.os, "_exit", fake_exit)
    monkeypatch.setattr(tagger, "_state", "loaded")
    monkeypatch.setattr(tagger, "_models", ("loaded",))
    monkeypatch.setattr(tagger, "_last_used", 0.0)
    with pytest.raises(SystemExit):
        tagger._watchdog_tick()
    assert exits == [0]
    assert tagger._models is not None  # no in-process unload happened first


def test_exit_mode_lazy_idle_never_exits(tagger, monkeypatch) -> None:
    # The fresh post-exit process sits at "idle" holding no GPU; the watchdog
    # must not exit-loop it.
    exits = []
    monkeypatch.setattr(tagger, "IDLE_UNLOAD_EXIT", True)
    monkeypatch.setattr(tagger, "IDLE_UNLOAD_SECONDS", 1)
    monkeypatch.setattr(tagger.os, "_exit", lambda code: exits.append(code))
    monkeypatch.setattr(tagger, "_state", "idle")
    monkeypatch.setattr(tagger, "_last_used", 0.0)
    tagger._watchdog_tick()
    assert exits == []


def test_exit_mode_starts_lazy_and_healthy(tagger, monkeypatch) -> None:
    spawned = []

    class FakeThread:
        def __init__(self, *, target, daemon):
            spawned.append(target)

        def start(self) -> None:
            pass

    monkeypatch.setattr(tagger, "IDLE_UNLOAD_EXIT", True)
    monkeypatch.setattr(tagger.threading, "Thread", FakeThread)
    tagger._startup()
    assert tagger._state == "idle"
    assert tagger._load_models_locked not in spawned
    assert tagger.health().status_code == 200


def test_health_is_200_when_idle_unloaded(tagger, monkeypatch) -> None:
    monkeypatch.setattr(tagger, "_state", "idle")
    monkeypatch.setattr(tagger, "_models", None)
    response = tagger.health()
    assert response.status_code == 200
    assert b"idle-unloaded" in response.body

    monkeypatch.setattr(tagger, "_state", "loading")
    assert tagger.health().status_code == 503
