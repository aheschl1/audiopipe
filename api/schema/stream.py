"""Wire frames for the streaming upload websocket.

The protocol these implement is specified in docs/streaming-protocol.md.
Text frames carry one JSON message discriminated on ``type``. Binary frames
differ by version: v1 sends bare enveloped chunks (a 4-byte big-endian header
length, the JSON-encoded :class:`ChunkHeader`, then the resource payload);
v2 prefixes every binary frame with a discriminator byte — a lean fixed-header
audio frame carrying raw PCM, or the v1 envelope for everything else.
"""

import struct
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

PROTOCOL_VERSION = 2
SUPPORTED_VERSIONS = (1, 2)

_HEADER_LEN_SIZE = 4

# v2 binary frame discriminators.
FRAME_AUDIO = 0x01
FRAME_ENVELOPE = 0x02

FLAG_CAPTURED_AT = 0x01
_KNOWN_FLAGS = FLAG_CAPTURED_AT
_AUDIO_FIXED_HEADER = 6  # discriminator + flags + u32 sequence


class FrameError(ValueError):
    """A frame that does not decode to a valid protocol message."""


# client -> server


class StreamDefaults(BaseModel):
    """Per-connection defaults applied to **audio** chunks that omit them.

    The field names say so: codec/sample_rate_hz/channels are PCM parameters.
    A chunk of another resource resolves its content type from its own header
    or its resource type's default, never from here.
    """

    model_config = ConfigDict(frozen=True)

    content_type: str = "application/octet-stream"
    codec: str | None = None
    sample_rate_hz: int | None = None
    channels: int | None = None


class Hello(BaseModel):
    """The required first frame.

    Credentials normally travel in the upgrade request's Authorization header;
    ``token`` is the fallback for clients whose websocket cannot set headers
    (browsers, embedded runtimes). It is ignored when the upgrade carried a
    valid header.
    """

    model_config = ConfigDict(frozen=True)

    type: Literal["hello"]
    version: int
    token: str | None = None
    defaults: StreamDefaults = StreamDefaults()
    effects: tuple[str, ...] = ()  # reserved: no effects exist yet


class Finish(BaseModel):
    """End the stream and the session; the server drains, acks, and closes."""

    model_config = ConfigDict(frozen=True)

    type: Literal["finish"]
    ended_at: datetime | None = None


ClientFrame = Annotated[Hello | Finish, Field(discriminator="type")]

_client_frame: TypeAdapter[Hello | Finish] = TypeAdapter(ClientFrame)


def parse_client_frame(text: str) -> Hello | Finish:
    """Decode a client text frame, raising :class:`FrameError` when invalid."""
    try:
        return _client_frame.validate_json(text)
    except ValidationError as exc:
        raise FrameError(f"invalid client frame: {exc}") from exc


class ChunkHeader(BaseModel):
    """The JSON prefix inside a binary chunk frame.

    ``resource`` names the payload's resource type; the default keeps every
    pre-resource client a valid audio stream. Sequences share one space per
    session regardless of resource.
    """

    model_config = ConfigDict(frozen=True)

    sequence: Annotated[int, Field(ge=0)]
    resource: str = "audio"
    captured_at: datetime | None = None
    duration_ms: Annotated[int, Field(ge=0)] | None = None
    content_type: str | None = None  # overrides the hello default
    checksum_sha256: str | None = None  # hex; verified by the server if present
    metadata: dict[str, Any] = Field(default_factory=dict)


def encode_chunk(header: ChunkHeader, payload: bytes) -> bytes:
    """Build a binary chunk frame. The client side of :func:`decode_chunk`."""
    head = header.model_dump_json(exclude_none=True).encode()
    return len(head).to_bytes(_HEADER_LEN_SIZE, "big") + head + payload


def decode_chunk(message: bytes) -> tuple[ChunkHeader, bytes]:
    """Split a binary chunk frame, raising :class:`FrameError` when malformed."""
    if len(message) < _HEADER_LEN_SIZE:
        raise FrameError("chunk frame shorter than its length prefix")
    header_len = int.from_bytes(message[:_HEADER_LEN_SIZE], "big")
    body_start = _HEADER_LEN_SIZE + header_len
    if body_start > len(message):
        raise FrameError(f"header length {header_len} exceeds the frame")
    try:
        header = ChunkHeader.model_validate_json(message[_HEADER_LEN_SIZE:body_start])
    except ValidationError as exc:
        raise FrameError(f"invalid chunk header: {exc}") from exc
    return header, message[body_start:]


class AudioFrame(BaseModel):
    """A decoded v2 audio frame: raw PCM addressed by a frame sequence."""

    model_config = ConfigDict(frozen=True)

    sequence: Annotated[int, Field(ge=0)]
    captured_at: datetime | None = None
    pcm: bytes


def encode_audio_frame(sequence: int, pcm: bytes, captured_at: datetime | None = None) -> bytes:
    """Build a v2 audio frame. The client side of :func:`decode_v2_frame`."""
    flags = 0
    stamp = b""
    if captured_at is not None:
        flags |= FLAG_CAPTURED_AT
        stamp = struct.pack(">Q", int(captured_at.timestamp() * 1000))
    return bytes((FRAME_AUDIO, flags)) + struct.pack(">I", sequence) + stamp + pcm


def decode_v2_frame(message: bytes) -> AudioFrame | tuple[ChunkHeader, bytes]:
    """Split a v2 binary frame, raising :class:`FrameError` when malformed."""
    if not message:
        raise FrameError("empty binary frame")
    discriminator = message[0]
    if discriminator == FRAME_ENVELOPE:
        return decode_chunk(message[1:])
    if discriminator != FRAME_AUDIO:
        raise FrameError(f"unknown frame discriminator 0x{discriminator:02x}")
    if len(message) < _AUDIO_FIXED_HEADER:
        raise FrameError("audio frame shorter than its fixed header")
    flags = message[1]
    if flags & ~_KNOWN_FLAGS:
        raise FrameError(f"unknown audio frame flags 0x{flags:02x}")
    (sequence,) = struct.unpack_from(">I", message, 2)
    body_start = _AUDIO_FIXED_HEADER
    captured_at = None
    if flags & FLAG_CAPTURED_AT:
        if len(message) < body_start + 8:
            raise FrameError("audio frame shorter than its captured_at stamp")
        (epoch_ms,) = struct.unpack_from(">Q", message, body_start)
        captured_at = datetime.fromtimestamp(epoch_ms / 1000, tz=UTC)
        body_start += 8
    return AudioFrame(sequence=sequence, captured_at=captured_at, pcm=message[body_start:])


# server -> client
# Every event is a JSON text frame with a ``type``; clients must ignore types
# they do not recognise, which is how future effect events stay compatible.


class AckWindow(BaseModel):
    model_config = ConfigDict(frozen=True)

    chunks: int
    seconds: float


class StreamLimits(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_chunk_bytes: int
    max_audio_frame_bytes: int | None = Field(
        default=None, description="Per-frame PCM cap; v2 connections only."
    )


class Welcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["welcome"] = "welcome"
    version: int = Field(default=PROTOCOL_VERSION, description="The negotiated version.")
    session_id: UUID
    next_sequence: int = Field(description="One past the highest stored sequence.")
    ack_window: AckWindow
    limits: StreamLimits
    effects: tuple[str, ...] = ()
    resources: tuple[str, ...] = Field(
        default=(),
        description="Resource types this server accepts in chunk headers.",
    )


class Ack(BaseModel):
    """Cumulative: every sequence at or below ``through`` is durably stored."""

    model_config = ConfigDict(frozen=True)

    type: Literal["ack"] = "ack"
    through: int
    count: int = Field(description="Chunks stored since the previous ack.")
    bytes: int
    duplicates: tuple[int, ...] = ()


class StreamError(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["error"] = "error"
    scope: Literal["chunk", "session"]
    code: str
    detail: str
    sequence: int | None = None


class Rotate(BaseModel):
    """The session was split on purpose: open a fresh one and reconnect.

    Sent just before the ``session_ended`` error and the 4409 close, so a
    client that predates this event still lands on the close path it already
    handles. Chunks above ``through`` were not stored; retransmit them into
    the successor session's sequence space.
    """

    model_config = ConfigDict(frozen=True)

    type: Literal["rotate"] = "rotate"
    through: int


class Bye(BaseModel):
    """The last event before the server closes the socket."""

    model_config = ConfigDict(frozen=True)

    type: Literal["bye"] = "bye"
    reason: Literal["finished", "shutdown", "idle"]
    through: int


class EffectEvent(BaseModel):
    """Output of a live pipeline the client enabled via ``hello.effects``.

    ``event`` and ``data`` mean whatever the effect says they mean; the stub
    counter emits ``started`` and ``finished``.
    """

    model_config = ConfigDict(frozen=True)

    type: Literal["effect"] = "effect"
    effect: str
    event: str
    sequence: int | None = None
    data: dict[str, Any] = Field(default_factory=dict)


ServerEvent = Welcome | Ack | StreamError | Rotate | Bye | EffectEvent

_server_event: TypeAdapter[ServerEvent] = TypeAdapter(
    Annotated[ServerEvent, Field(discriminator="type")]
)


def parse_server_event(text: str) -> ServerEvent:
    """Decode a server event; for clients and tests, the server only encodes."""
    try:
        return _server_event.validate_json(text)
    except ValidationError as exc:
        raise FrameError(f"invalid server event: {exc}") from exc
