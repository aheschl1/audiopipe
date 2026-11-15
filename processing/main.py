"""The processing-worker entrypoint.

    uv run python -m processing                     # every registered pipeline
    uv run python -m processing --only session-stats

One supervisor process; one child process per pipeline (see
docs/processing-pipelines.md).
"""

import asyncio
import logging

import click

from database.pipe import DatabasePipe, close_pool
from processing import registry
from processing.config import get_settings
from processing.supervisor import Supervisor


async def _ping() -> None:
    """Fail fast on a bad DATABASE_* config, before any child spawns."""
    try:
        async with DatabasePipe() as pipe:
            await pipe.ping()
    finally:
        # The children build their own pools; the parent keeps none open.
        await close_pool()


@click.command()
@click.option(
    "--only",
    "only",
    multiple=True,
    help="Run a subset of pipelines (repeatable).",
)
@click.option(
    "--log-level",
    default="info",
    show_default=True,
    type=click.Choice(["critical", "error", "warning", "info", "debug"]),
)
def serve(only: tuple[str, ...], log_level: str) -> None:
    """Run the context processing workers."""
    known = registry.names()
    for name in only:
        if name not in known:
            raise click.UsageError(
                f"unknown pipeline {name!r}; registered: {', '.join(known)}"
            )
    logging.basicConfig(
        level=log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(_ping())
    Supervisor(tuple(only) or known, get_settings(), log_level=log_level).run()


if __name__ == "__main__":
    serve()
