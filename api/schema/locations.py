"""Response models for location points."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from database.repos.locations import LocationPointRow, TrackPointRow


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


class TrackPointRead(BaseModel):
    """One fix of a decimated track, as lean as the map needs.

    ``at_ms`` carries the same session-timeline semantics as
    :class:`LocationPointRead.at_ms` (including possible negativity) — it is
    the seek target for opening the session at this fix. Wall-clock time is
    the session's ``started_at`` plus ``at_ms``; coordinates are rounded to
    5 decimals (~1 m, at the limit of consumer GPS). Tracks return thousands
    of these per response, so every field earns its bytes.
    """

    model_config = ConfigDict(frozen=True)

    at_ms: int
    lat: float
    lon: float

    @classmethod
    def from_model(cls, row: TrackPointRow) -> "TrackPointRead":
        return cls(at_ms=row.at_ms, lat=round(row.lat, 5), lon=round(row.lon, 5))


class SessionTrackRead(BaseModel):
    """One session's GPS track over the queried window.

    ``point_count`` and the bounds are exact (computed before decimation);
    ``points`` is the thinned polyline, oldest first, first and last fixes
    always included.
    """

    model_config = ConfigDict(frozen=True)

    session_id: UUID
    label: str | None
    device: str | None
    started_at: datetime
    point_count: int
    min_lat: float
    max_lat: float
    min_lon: float
    max_lon: float
    points: list[TrackPointRead]
