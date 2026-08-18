"""The explicit list of live pipelines, mirroring processing/registry.py."""

from live.base import LivePipeline
from live.pipelines import CountingPipeline

LIVE_PIPELINES: tuple[type[LivePipeline], ...] = (CountingPipeline,)

_BY_NAME = {cls.name: cls for cls in LIVE_PIPELINES}


def names() -> tuple[str, ...]:
    return tuple(cls.name for cls in LIVE_PIPELINES)


def build(name: str) -> LivePipeline:
    return _BY_NAME[name]()


if len(_BY_NAME) != len(LIVE_PIPELINES):
    raise RuntimeError("duplicate live pipeline names")
