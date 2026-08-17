"""The HTTP application and its entrypoint.

    uv run python -m api                      # serve
    uv run python -m api --reload --port 9000
    uv run uvicorn api.main:app               # for tooling that wants the app

"""

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from datetime import datetime, time, timedelta
from typing import AsyncIterator

import click
import uvicorn
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from fastapi.middleware.cors import CORSMiddleware

from api.config import get_settings
from api.routes import (
    ab,
    auth,
    health,
    resources,
    search,
    segments,
    sessions,
    speakers,
    stream,
    users,
)
from api.schema.common import ErrorResponse
from api.schema.stream_export import build_schema
from database.exceptions import DatabaseError, NotFoundError
from database.pipe import DatabasePipe, close_pool
from storage.base import BlobNotFoundError
from storage.pipe import close_blob_client

logger = logging.getLogger(__name__)

API_PREFIX = "/v1"


def _last_rotation_instant(now: datetime, rotate_at: time) -> datetime:
    """The most recent occurrence of ``rotate_at`` at or before ``now``."""
    candidate = now.replace(
        hour=rotate_at.hour, minute=rotate_at.minute, second=0, microsecond=0
    )
    return candidate if candidate <= now else candidate - timedelta(days=1)


async def _sweep_sessions() -> None:
    """End sessions abandoned mid-stream, and rotate on the daily schedule.

    Streaming and ingest heartbeat ``updated_at``; a session that stops
    heartbeating was dropped without a ``finish``, and is closed here once the
    stale window passes. With API_SESSION_ROTATE_AT set, every open session
    started before the day's rotation instant is split too, which starts its
    processing and tells a connected client to reopen. Every pass is
    idempotent — rotation only matches sessions started before the cutoff —
    so multi-worker deploys sweep concurrently without coordination, and a
    restart catches up on the first pass.
    """
    settings = get_settings()
    rotate_at = (
        time.fromisoformat(settings.session_rotate_at)
        if settings.session_rotate_at is not None
        else None
    )
    while True:
        await asyncio.sleep(settings.session_sweep_interval_seconds)
        try:
            async with DatabasePipe() as pipe:
                ended = await pipe.sessions.end_stale(settings.session_stale_seconds)
                if ended:
                    logger.info("ended %d stale recording session(s)", ended)
                if rotate_at is not None:
                    cutoff = _last_rotation_instant(datetime.now().astimezone(), rotate_at)
                    split = await pipe.sessions.split_started_before(cutoff)
                    if split:
                        logger.info("rotated %d recording session(s)", split)
        except Exception:
            logger.exception("session sweep failed; will retry")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Open the pool at boot so a bad DATABASE_* config fails here, not on the
    # first request.
    async with DatabasePipe() as pipe:
        await pipe.ping()
    sweeper = asyncio.create_task(_sweep_sessions())
    yield
    sweeper.cancel()
    with suppress(asyncio.CancelledError):
        await sweeper
    await close_blob_client()
    await close_pool()


def _error(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=ErrorResponse(detail=detail).model_dump())


def create_app() -> FastAPI:
    app = FastAPI(
        title="audio-pipeline",
        version="0.1.0",
        summary=(
            "Recording sessions and their audio segments. Audio is uploaded over "
            "the streaming websocket described in docs/streaming-protocol.md."
        ),
        lifespan=lifespan,
        responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    )

    # Auth is a bearer header, never a cookie, so a wildcard origin grants
    # nothing beyond what any HTTP client already has — and the miniapp
    # WebView's fetches arrive cross-origin.
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
    )

    # Domain errors from the repositories and services map to HTTP here, so no
    # route body needs a try/except.
    @app.exception_handler(NotFoundError)
    async def _not_found(request: Request, exc: NotFoundError) -> JSONResponse:
        return _error(status.HTTP_404_NOT_FOUND, str(exc))

    @app.exception_handler(BlobNotFoundError)
    async def _blob_missing(request: Request, exc: BlobNotFoundError) -> JSONResponse:
        # A row pointing at a missing object is an inconsistency, not a plain
        # miss: logged even though the caller only sees 404.
        logger.error("segment row references a missing object: %s", exc.key)
        return _error(status.HTTP_404_NOT_FOUND, "the stored audio for this segment is missing")

    @app.exception_handler(DatabaseError)
    async def _database_error(request: Request, exc: DatabaseError) -> JSONResponse:
        logger.exception("database error serving %s", request.url.path)
        return _error(status.HTTP_500_INTERNAL_SERVER_ERROR, "internal error")

    # Built once: the frame models are fixed for the process lifetime.
    stream_schema = build_schema()

    @app.get(
        "/stream-schema.json",
        tags=["stream"],
        summary="Frame schema for the streaming websocket",
    )
    async def stream_schema_json() -> dict:
        """JSON Schema for the websocket frames (docs/streaming-protocol.md).

        The websocket itself cannot appear in OpenAPI; clients generate their
        frame types from this document instead.
        """
        return stream_schema

    app.include_router(health.router)
    app.include_router(auth.router, prefix=API_PREFIX)
    app.include_router(users.router, prefix=API_PREFIX)
    app.include_router(sessions.router, prefix=API_PREFIX)
    app.include_router(segments.router, prefix=API_PREFIX)
    app.include_router(resources.router, prefix=API_PREFIX)
    app.include_router(speakers.router, prefix=API_PREFIX)
    app.include_router(search.router, prefix=API_PREFIX)
    app.include_router(stream.router, prefix=API_PREFIX)
    app.include_router(ab.router, prefix=API_PREFIX)
    return app


app = create_app()

# An import string, not `app`: uvicorn re-imports the module in each subprocess
# for --reload and --workers.
APP_PATH = "api.main:app"


@click.command()
@click.option("--host", default="127.0.0.1", show_default=True, help="Interface to bind.")
@click.option("--port", default=8000, show_default=True, type=int)
@click.option("--reload", is_flag=True, help="Restart on code changes.")
@click.option("--workers", default=1, show_default=True, type=int, help="Worker processes.")
@click.option(
    "--log-level",
    default="info",
    show_default=True,
    type=click.Choice(["critical", "error", "warning", "info", "debug", "trace"]),
)
def serve(host: str, port: int, reload: bool, workers: int, log_level: str) -> None:
    """Run the audio-pipeline HTTP API."""
    if reload and workers > 1:
        raise click.UsageError("--reload cannot be combined with --workers")
    uvicorn.run(
        APP_PATH,
        host=host,
        port=port,
        reload=reload,
        workers=workers if not reload else None,
        log_level=log_level,
    )


if __name__ == "__main__":
    serve()
