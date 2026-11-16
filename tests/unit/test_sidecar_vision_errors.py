"""The vision sidecar's error split: a bad input is 422, a failing model 503."""

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def vision():
    spec = importlib.util.spec_from_file_location(
        "vision_app_under_test", REPO_ROOT / "sidecars" / "vision" / "app.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        del sys.modules[spec.name]


def test_inference_failure_is_503(vision, monkeypatch) -> None:
    monkeypatch.setattr(vision, "_load", lambda: ("processor", "model", "cpu", "fp16"))

    def oom(*_):
        raise RuntimeError("CUDA out of memory")

    with pytest.raises(HTTPException) as caught:
        vision._run(oom)
    assert caught.value.status_code == 503


def test_load_failure_is_503(vision, monkeypatch) -> None:
    def broken():
        raise OSError("no checkpoint")

    monkeypatch.setattr(vision, "_load", broken)
    with pytest.raises(HTTPException) as caught:
        vision._run(lambda *_: None)
    assert caught.value.status_code == 503


def test_undecodable_upload_is_422(vision, monkeypatch) -> None:
    def bad(_blobs):
        raise ValueError("cannot identify image file")

    monkeypatch.setattr(vision, "_decode", bad)

    class Upload:
        class file:
            @staticmethod
            def read() -> bytes:
                return b"nope"

    with pytest.raises(HTTPException) as caught:
        vision.images([Upload()])
    assert caught.value.status_code == 422
