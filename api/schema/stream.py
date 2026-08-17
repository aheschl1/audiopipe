"""Wire frames for the streaming upload websocket.

The protocol these implement is specified in docs/streaming-protocol.md.
Text frames carry one JSON message discriminated on ``type``; the one binary
frame is a chunk: a 4-byte big-endian header length, the JSON-encoded
:class:`ChunkHeader`, then the resource payload (audio bytes, a location
batch, ...).
"""

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

PROTOCOL_VERSION = 1

_HEADER_LEN_SIZE = 4


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


class Welcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["welcome"] = "welcome"
    version: int = PROTOCOL_VERSION
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


ServerEvent = Welcome | Ack | StreamError | Rotate | Bye

_server_event: TypeAdapter[ServerEvent] = TypeAdapter(
    Annotated[ServerEvent, Field(discriminator="type")]
)


def parse_server_event(text: str) -> ServerEvent:
    """Decode a server event; for clients and tests, the server only encodes."""
    try:
        return _server_event.validate_json(text)
    except ValidationError as exc:
        raise FrameError(f"invalid server event: {exc}") from exc
