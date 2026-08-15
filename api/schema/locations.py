"""Response models for location points."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from database.repos.locations import LocationPointRow


class LocationPointRead(BaseModel):
    """One GPS fix, addressed on both clocks.

    ``at_ms`` is the position on its session's timeline (wall clock minus
    session start). It can drift from the audio-position time other timeline
    entries use when capture had gaps, and can be negative for a fix taken
    just before the session row was created.
    """

    model_config = ConfigDict(frozen=True)

    session_id: UUID
    segment_id: UUID = Field(description="The batch segment the point arrived in.")
    at_ms: int
    captured_at: datetime
    lat: float
    lon: float
    alt_m: float | None
    accuracy_m: float | None

    @classmethod
    def from_model(cls, row: LocationPointRow) -> "LocationPointRead":
        return cls(
            session_id=row.session_id,
            segment_id=row.segment_id,
            at_ms=row.at_ms,
            captured_at=row.captured_at,
            lat=row.lat,
            lon=row.lon,
            alt_m=row.alt_m,
            accuracy_m=row.accuracy_m,
        )
