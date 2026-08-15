"""The explicit resource registry.

Adding a resource is: add the :class:`~resources.base.Resource` enum member,
write the class, import it here, append an instance to ``RESOURCES``.
Mirrors ``processing/registry.py``.
"""

from resources.audio import AudioResource
from resources.base import Resource, ResourceType
from resources.location import LocationResource

RESOURCES: tuple[ResourceType, ...] = (
    AudioResource(),
    LocationResource(),
)

_BY_NAME = {resource.name: resource for resource in RESOURCES}


def names() -> tuple[Resource, ...]:
    return tuple(resource.name for resource in RESOURCES)


def get(name: str) -> ResourceType:
    """The registered resource type (KeyError if unknown)."""
    return _BY_NAME[name]


# The Resource enum and the registry must describe the same universe: every
# member implemented exactly once. (StrEnum makes a stray name statically
# improbable; this catches a member added without an implementation.)
if set(_BY_NAME) != set(Resource) or len(_BY_NAME) != len(RESOURCES):
    raise RuntimeError("RESOURCES must implement each Resource member exactly once")
