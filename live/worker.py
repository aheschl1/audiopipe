"""The live worker child process: UDS listener, one handler per connection."""

import asyncio
import logging
import os
import signal

from live.config import LiveSettings
from live.protocol import (
    MSG_AUDIO,
    MSG_HELLO,
    ProtocolError,
    SessionHello,
    decode_audio,
    decode_json,
    read_message,
)
from live.sessions import SessionStream

logger = logging.getLogger(__name__)


def run_worker(socket_path: str, log_level: str = "info") -> None:
    """Child entry point; module-level and string-argumented for spawn."""
    logging.basicConfig(
        level=log_level.upper(),
        format="%(asctime)s %(levelname)s live %(name)s: %(message)s",
    )
    asyncio.run(_worker_main(socket_path))


async def _worker_main(socket_path: str) -> None:
    settings = LiveSettings()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)

    os.makedirs(os.path.dirname(socket_path), mode=0o700, exist_ok=True)
    if os.path.exists(socket_path):
        os.unlink(socket_path)
    server = await asyncio.start_unix_server(
        lambda reader, writer: _serve_connection(reader, writer, settings),
        path=socket_path,
    )
    logger.info("live worker listening on %s", socket_path)
    async with server:
        await stop.wait()
    logger.info("live worker stopped")


async def _serve_connection(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, settings: LiveSettings
) -> None:
    """One tap connection: HELLO, then audio until EOF (the detach)."""
    session: SessionStream | None = None
    try:
        message = await read_message(reader)
        if message is None:
            return
        msg_type, body = message
        if msg_type != MSG_HELLO:
            raise ProtocolError(f"expected HELLO, got 0x{msg_type:02x}")
        hello = decode_json(SessionHello, body)
        session = SessionStream(hello, settings, writer)
        logger.info("attached session %s (effects %s)", hello.session_id, hello.effects)
        while (message := await read_message(reader)) is not None:
            msg_type, body = message
            if msg_type == MSG_AUDIO:
                sequence, pcm = decode_audio(body)
                await session.feed(sequence, pcm)
    except ProtocolError as exc:
        logger.warning("dropping tap connection: %s", exc)
    except Exception:
        logger.exception("tap connection failed")
    finally:
        if session is not None:
            await session.close()
        writer.close()
