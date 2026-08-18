"""Spawns and watches the live worker child from inside the API process.

An async sibling of processing/supervisor.py: same backoff/reset scheme, but
monitored from the API's event loop, and always the spawn context — forking a
running uvicorn/asyncio process is unsafe.
"""

import asyncio
import logging
import multiprocessing

from live.config import LiveSettings

logger = logging.getLogger(__name__)


class LiveWorkerSupervisor:
    def __init__(self, settings: LiveSettings, socket_path: str, log_level: str = "info") -> None:
        self._settings = settings
        self._socket_path = socket_path
        self._log_level = log_level
        self._process = None
        self._monitor: asyncio.Task | None = None

    def start(self) -> None:
        self._spawn()
        self._monitor = asyncio.create_task(self._watch())

    def _spawn(self) -> None:
        from live.worker import run_worker

        ctx = multiprocessing.get_context("spawn")
        self._process = ctx.Process(
            target=run_worker, args=(self._socket_path, self._log_level), name="live-worker"
        )
        self._process.start()
        logger.info("started live worker (pid %s)", self._process.pid)

    async def _watch(self) -> None:
        backoff = self._settings.restart_backoff_base_seconds
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        while True:
            await asyncio.sleep(1.0)
            if self._process.is_alive():
                continue
            if loop.time() - started_at >= self._settings.restart_reset_seconds:
                backoff = self._settings.restart_backoff_base_seconds
            logger.warning(
                "live worker exited (code %s); restarting in %.1fs",
                self._process.exitcode, backoff,
            )
            await asyncio.sleep(backoff)
            backoff = min(self._settings.restart_backoff_cap_seconds, backoff * 2)
            self._spawn()
            started_at = loop.time()

    async def stop(self) -> None:
        if self._monitor is not None:
            self._monitor.cancel()
        if self._process is None or not self._process.is_alive():
            return
        self._process.terminate()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, self._process.join, self._settings.shutdown_grace_seconds
        )
        if self._process.is_alive():
            logger.error("live worker ignored SIGTERM; killing")
            self._process.kill()
            await loop.run_in_executor(None, self._process.join)
        logger.info("live worker stopped")
