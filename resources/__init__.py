"""The resource type registry: what kinds of data a session can hold.

A *resource* is one stream of data captured against a session — audio and
location today. Each type declares how its chunks are validated and stored
(``blob`` in the object store, or ``inline`` on the segment row) and which
capabilities the API may offer for it. Pipelines and endpoints interrogate
the registry instead of assuming audio.
"""

from resources.base import (
    Resource,
    ResourceType,
    ResourceValidationError,
    ValidatedChunk,
)
from resources.registry import RESOURCES, get, names

__all__ = [
    "RESOURCES",
    "Resource",
    "ResourceType",
    "ResourceValidationError",
    "ValidatedChunk",
    "get",
    "names",
]
