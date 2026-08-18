"""UDS wire format between the API tap and the live worker.

One connection per streaming websocket connection: the API connects, sends
one HELLO, then AUDIO messages; the worker answers with EVENT messages. The
connection closing (either way) is the detach — there is no message for it.

Every message is ``[type u8][length u32 BE][body]``.
"""

import asyncio
import struct
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError

PROTOCOL_VERSION = 1

MSG_HELLO = 0x00
MSG_AUDIO = 0x01
MSG_EVENT = 0x10

_HEADER = struct.Struct(">BI")
MAX_MESSAGE_BYTES = 16 * 1024 * 1024


class ProtocolError(ValueError):
    """A message that does not decode."""


class SessionHello(BaseModel):
    """The tap's first message: what stream this connection carries."""

    model_config = ConfigDict(frozen=True)

    version: int
    session_id: UUID
    user_id: UUID
    sample_rate_hz: int
    channels: int
    effects: tuple[str, ...] = ()


class Event(BaseModel):
    """One live-pipeline event on its way back to the websocket."""

    model_config = ConfigDict(frozen=True)

    effect: str
    event: str
    sequence: int | None = None
    data: dict[str, Any] = {}


def encode_message(msg_type: int, body: bytes) -> bytes:
    return _HEADER.pack(msg_type, len(body)) + body


def encode_audio(sequence: int, pcm: bytes) -> bytes:
    return encode_message(MSG_AUDIO, struct.pack(">I", sequence) + pcm)


def decode_audio(body: bytes) -> tuple[int, bytes]:
    if len(body) < 4:
        raise ProtocolError("audio message shorter than its sequence")
    return struct.unpack_from(">I", body)[0], body[4:]


def decode_json(model: type[BaseModel], body: bytes) -> Any:
    try:
        return model.model_validate_json(body)
    except ValidationError as exc:
        raise ProtocolError(f"invalid {model.__name__}: {exc}") from exc


async def read_message(reader: asyncio.StreamReader) -> tuple[int, bytes] | None:
    """The next message, or None at a clean or torn EOF."""
    try:
        header = await reader.readexactly(_HEADER.size)
    except (asyncio.IncompleteReadError, ConnectionResetError):
        return None
    msg_type, length = _HEADER.unpack(header)
    if length > MAX_MESSAGE_BYTES:
        raise ProtocolError(f"message of {length} bytes exceeds {MAX_MESSAGE_BYTES}")
    try:
        body = await reader.readexactly(length)
    except (asyncio.IncompleteReadError, ConnectionResetError):
        return None
    return msg_type, body
