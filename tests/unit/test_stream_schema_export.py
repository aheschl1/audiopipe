"""The frame schema the server serves at /stream-schema.json.

The endpoint is covered e2e in test_openapi.py; these check the document's
shape without a server.
"""

import json

from api.schema.stream_export import build_schema


def test_schema_exposes_every_root() -> None:
    schema = build_schema()
    assert set(schema["properties"]) == {"clientFrame", "serverEvent", "chunkHeader"}
    for frame in (
        "Hello",
        "Finish",
        "Welcome",
        "Ack",
        "StreamError",
        "Rotate",
        "Bye",
        "EffectEvent",
        "ChunkHeader",
    ):
        assert frame in schema["$defs"], f"{frame} missing from $defs"


def test_schema_is_json_serializable_and_stable() -> None:
    once = json.dumps(build_schema(), sort_keys=True)
    again = json.dumps(build_schema(), sort_keys=True)
    assert once == again
