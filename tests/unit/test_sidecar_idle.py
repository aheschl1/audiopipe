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


def test_failed_reload_stays_idle_so_the_next_request_retries(tagger, monkeypatch) -> None:
    def failing_load() -> None:
        tagger._state = "failed"
        tagger._load_error = "boom"

    monkeypatch.setattr(tagger, "_load_models", failing_load)
    monkeypatch.setattr(tagger, "_state", "idle")
    monkeypatch.setattr(tagger, "_models", None)
    with pytest.raises(tagger.ModelsUnavailable, match="boom"):
        tagger._ensure_loaded()
    assert tagger._state == "idle"


def test_failed_boot_raises_with_the_load_error(tagger, monkeypatch) -> None:
    monkeypatch.setattr(tagger, "_state", "failed")
    monkeypatch.setattr(tagger, "_load_error", "no cuda")
    with pytest.raises(tagger.ModelsUnavailable, match="no cuda"):
        tagger._ensure_loaded()


def test_health_is_200_when_idle_unloaded(tagger, monkeypatch) -> None:
    monkeypatch.setattr(tagger, "_state", "idle")
    monkeypatch.setattr(tagger, "_models", None)
    response = tagger.health()
    assert response.status_code == 200
    assert b"idle-unloaded" in response.body

    monkeypatch.setattr(tagger, "_state", "loading")
    assert tagger.health().status_code == 503
