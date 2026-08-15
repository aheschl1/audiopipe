"""Location: inline-stored batches of GPS fixes.

A chunk's payload is JSON — ``{"points": [{lat, lon, t, alt_m?, accuracy_m?},
…]}`` with ``t`` in epoch milliseconds (compact for battery-constrained
clients, and trivial to address from SQL). The parsed batch is stored on the
segment row itself; no blob is written.

Every stored row spans its batch: ``captured_at`` is the first point's time
and ``duration_ms`` the first-to-last span (derived here when the header
omits them). Cross-session wall-clock queries prefilter rows on that pair
before unnesting points, so the derivation is a contract, not a convenience.
"""

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from resources.base import Resource, ResourceType, ResourceValidationError, ValidatedChunk

# One batch per periodic upload; far above any sane cadence, well below
# anything that could strain a JSONB row.
MAX_POINTS_PER_BATCH = 10_000


class LocationPoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    lat: Annotated[float, Field(ge=-90, le=90)]
    lon: Annotated[float, Field(ge=-180, le=180)]
    t: Annotated[int, Field(ge=0, description="Fix time, epoch milliseconds.")]
    alt_m: float | None = None
    accuracy_m: Annotated[float, Field(ge=0)] | None = None


class LocationPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    points: Annotated[
        tuple[LocationPoint, ...],
        Field(min_length=1, max_length=MAX_POINTS_PER_BATCH),
    ]


class LocationResource(ResourceType):
    name = Resource.LOCATION
    storage = "inline"
    default_content_type = "application/json"
    wall_clock_queryable = True
    timeline_events = True

    def validate_chunk(
        self,
        payload: bytes,
        *,
        content_type: str | None,
        declared_attrs: Mapping[str, Any],
        captured_at: datetime | None,
        duration_ms: int | None,
    ) -> ValidatedChunk:
        if content_type not in (None, self.default_content_type):
            raise ResourceValidationError(
                f"location chunks are {self.default_content_type}, not {content_type}"
            )
        if declared_attrs:
            raise ResourceValidationError(
                f"location declares no attrs, got {sorted(declared_attrs)}"
            )
        try:
            batch = LocationPayload.model_validate(json.loads(payload))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ResourceValidationError(f"payload is not JSON: {exc}") from exc
        except ValidationError as exc:
            raise ResourceValidationError(f"invalid location batch: {exc}") from exc

        times = [point.t for point in batch.points]
        if times != sorted(times):
            raise ResourceValidationError("points must be ordered by t")

        return ValidatedChunk(
            attrs={},
            # Normalized dump, not the raw bytes: SQL addresses a known shape.
            payload=batch.model_dump(mode="json", exclude_none=True),
            content_type=self.default_content_type,
            captured_at=captured_at
            or datetime.fromtimestamp(times[0] / 1000, tz=UTC),
            duration_ms=duration_ms if duration_ms is not None else times[-1] - times[0],
        )
