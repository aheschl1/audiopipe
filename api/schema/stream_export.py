"""The streaming-protocol frame schema, for client codegen.

Built live from the pydantic models and served by the API at
``/stream-schema.json`` (next to ``/openapi.json``), so clients generate types
against whatever the server they target actually speaks. ``ClientFrame`` /
``ServerEvent`` / ``ChunkHeader`` are the roots to generate from.

    uv run python -m api.schema.stream_export   # print it without a server
"""

import json
from typing import Annotated, Any

from pydantic import Field, TypeAdapter

from api.schema.stream import PROTOCOL_VERSION, ChunkHeader, ClientFrame, ServerEvent

_ROOTS: dict[str, TypeAdapter[Any]] = {
    "ClientFrame": TypeAdapter(ClientFrame),
    "ServerEvent": TypeAdapter(Annotated[ServerEvent, Field(discriminator="type")]),
    "ChunkHeader": TypeAdapter(ChunkHeader),
}


def build_schema() -> dict[str, Any]:
    defs: dict[str, Any] = {}
    for name, adapter in _ROOTS.items():
        schema = adapter.json_schema(ref_template="#/$defs/{model}")
        defs.update(schema.pop("$defs", {}))
        defs[name] = schema
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "StreamProtocol",
        "description": (
            "Wire frames for the context streaming websocket "
            f"(docs/streaming-protocol.md), protocol version {PROTOCOL_VERSION}."
        ),
        "type": "object",
        "properties": {
            "clientFrame": {"$ref": "#/$defs/ClientFrame"},
            "serverEvent": {"$ref": "#/$defs/ServerEvent"},
            "chunkHeader": {"$ref": "#/$defs/ChunkHeader"},
        },
        "$defs": defs,
    }


def main() -> None:
    # Same key order the server serves, so the two outputs diff cleanly.
    print(json.dumps(build_schema(), indent=2))


if __name__ == "__main__":
    main()
