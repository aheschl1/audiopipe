"""The location query surface: in-session timeline windows and the
cross-session wall-clock query."""

from datetime import UTC, datetime

import httpx

from database.pipe import DatabasePipe
from database.schema.sessions import SessionCreate
from tests.e2e.conftest import Account, ingest_location, make_session


def _ms(at: datetime) -> int:
    return int(at.timestamp() * 1000)


async def _session_started_at(
    account: Account, started_at: datetime, label: str | None = None
):
    async with DatabasePipe() as pipe:
        return await pipe.sessions.create(
            SessionCreate(user_id=account.user.id, started_at=started_at, label=label)
        )


async def test_session_points_window_partitions(
    client: httpx.AsyncClient, account: Account
) -> None:
    start = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
    session = await _session_started_at(account, start)
    base = _ms(start)
    # Two batches: fixes at 1s, 2s and 10s, 11s into the session.
    await ingest_location(
        session.id,
        [
            {"lat": 51.0, "lon": -114.0, "t": base + 1_000},
            {"lat": 51.1, "lon": -114.1, "t": base + 2_000},
        ],
    )
    await ingest_location(
        session.id,
        [
            {"lat": 51.2, "lon": -114.2, "t": base + 10_000},
            {"lat": 51.3, "lon": -114.3, "t": base + 11_000},
        ],
    )

    everything = await client.get(
        f"/v1/sessions/{session.id}/resources/location/points", headers=account.headers
    )
    assert everything.status_code == 200
    items = everything.json()["items"]
    assert [item["at_ms"] for item in items] == [1_000, 2_000, 10_000, 11_000]
    assert items[0]["lat"] == 51.0
    assert items[0]["captured_at"].startswith("2026-08-14T12:00:01")

    # Half-open windows partition: [0, 2000) and [2000, 12000) share nothing.
    first = await client.get(
        f"/v1/sessions/{session.id}/resources/location/points",
        params={"from_ms": 0, "to_ms": 2_000},
        headers=account.headers,
    )
    second = await client.get(
        f"/v1/sessions/{session.id}/resources/location/points",
        params={"from_ms": 2_000, "to_ms": 12_000},
        headers=account.headers,
    )
    assert [i["at_ms"] for i in first.json()["items"]] == [1_000]
    assert [i["at_ms"] for i in second.json()["items"]] == [2_000, 10_000, 11_000]


async def test_session_points_paginate(
    client: httpx.AsyncClient, account: Account
) -> None:
    start = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
    session = await _session_started_at(account, start)
    base = _ms(start)
    await ingest_location(
        session.id,
        [{"lat": 51.0, "lon": -114.0, "t": base + i * 1_000} for i in range(5)],
    )

    page_one = await client.get(
        f"/v1/sessions/{session.id}/resources/location/points",
        params={"limit": 3},
        headers=account.headers,
    )
    body = page_one.json()
    assert [i["at_ms"] for i in body["items"]] == [0, 1_000, 2_000]
    assert body["has_more"] is True

    page_two = await client.get(
        f"/v1/sessions/{session.id}/resources/location/points",
        params={"limit": 3, "offset": 3},
        headers=account.headers,
    )
    body = page_two.json()
    assert [i["at_ms"] for i in body["items"]] == [3_000, 4_000]
    assert body["has_more"] is False


async def test_session_without_location_serves_empty_page(
    client: httpx.AsyncClient, account: Account
) -> None:
    session = await make_session(account)
    response = await client.get(
        f"/v1/sessions/{session.id}/resources/location/points", headers=account.headers
    )
    assert response.status_code == 200
    assert response.json() == {"items": [], "limit": 50, "offset": 0, "has_more": False}


async def test_wall_clock_query_spans_sessions(
    client: httpx.AsyncClient, account: Account, other_account: Account
) -> None:
    noon = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
    one = await _session_started_at(account, noon)
    two = await _session_started_at(account, datetime(2026, 8, 14, 15, 0, tzinfo=UTC))
    await ingest_location(one.id, [{"lat": 51.0, "lon": -114.0, "t": _ms(noon) + 5_000}])
    await ingest_location(
        two.id,
        [{"lat": 52.0, "lon": -113.0, "t": _ms(noon) + 3 * 3_600_000 + 5_000}],
    )
    # Someone else's fix in the same window must never surface.
    theirs = await _session_started_at(other_account, noon)
    await ingest_location(theirs.id, [{"lat": 0.0, "lon": 0.0, "t": _ms(noon) + 5_000}])

    everything = await client.get(
        "/v1/resources/location/points", headers=account.headers
    )
    items = everything.json()["items"]
    assert [(i["session_id"], i["lat"]) for i in items] == [
        (str(one.id), 51.0),
        (str(two.id), 52.0),
    ]

    windowed = await client.get(
        "/v1/resources/location/points",
        params={
            "from_ms": _ms(datetime(2026, 8, 14, 14, 0, tzinfo=UTC)),
            "to_ms": _ms(datetime(2026, 8, 14, 16, 0, tzinfo=UTC)),
        },
        headers=account.headers,
    )
    assert [i["lat"] for i in windowed.json()["items"]] == [52.0]

    narrowed = await client.get(
        "/v1/resources/location/points",
        params={"session_id": str(one.id)},
        headers=account.headers,
    )
    assert [i["lat"] for i in narrowed.json()["items"]] == [51.0]

    # A foreign session id filters within your scope: it matches nothing.
    foreign = await client.get(
        "/v1/resources/location/points",
        params={"session_id": str(theirs.id)},
        headers=account.headers,
    )
    assert foreign.json()["items"] == []


async def test_wall_clock_query_rejects_negative_window(
    client: httpx.AsyncClient, account: Account
) -> None:
    response = await client.get(
        "/v1/resources/location/points",
        params={"from_ms": -1},
        headers=account.headers,
    )
    assert response.status_code == 422


async def test_wall_clock_prefilter_trims_batch_edges(
    client: httpx.AsyncClient, account: Account
) -> None:
    """A window inside one batch returns only the covered points — the
    row-level span prefilter must not skip the batch, and the point-level
    filter must trim it."""
    noon = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
    session = await _session_started_at(account, noon)
    base = _ms(noon)
    await ingest_location(
        session.id,
        [{"lat": 51.0 + i, "lon": -114.0, "t": base + i * 60_000} for i in range(5)],
    )

    inside = await client.get(
        "/v1/resources/location/points",
        params={"from_ms": base + 60_000, "to_ms": base + 180_000},
        headers=account.headers,
    )
    assert [i["lat"] for i in inside.json()["items"]] == [52.0, 53.0]


async def test_tracks_group_per_session(
    client: httpx.AsyncClient, account: Account
) -> None:
    noon = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
    later = datetime(2026, 8, 14, 15, 0, tzinfo=UTC)
    one = await _session_started_at(account, noon, label="morning walk")
    two = await _session_started_at(account, later)
    await ingest_location(
        one.id,
        [
            {"lat": 51.0, "lon": -114.0, "t": _ms(noon) + 1_000},
            {"lat": 51.2, "lon": -114.4, "t": _ms(noon) + 2_000},
        ],
    )
    await ingest_location(two.id, [{"lat": 52.0, "lon": -113.0, "t": _ms(later) + 5_000}])

    response = await client.get(
        "/v1/resources/location/tracks",
        params={"from_ms": _ms(noon), "to_ms": _ms(later) + 3_600_000},
        headers=account.headers,
    )
    assert response.status_code == 200
    tracks = response.json()
    # A plain list, not a Page envelope; sessions contiguous, oldest first.
    assert isinstance(tracks, list)
    assert [t["session_id"] for t in tracks] == [str(one.id), str(two.id)]
    first = tracks[0]
    assert first["label"] == "morning walk"
    assert first["point_count"] == 2
    assert (first["min_lat"], first["max_lat"]) == (51.0, 51.2)
    assert (first["min_lon"], first["max_lon"]) == (-114.4, -114.0)
    assert [p["at_ms"] for p in first["points"]] == [1_000, 2_000]
    assert tracks[1]["points"][0]["lat"] == 52.0


async def test_tracks_decimate_keeping_endpoints(
    client: httpx.AsyncClient, account: Account
) -> None:
    noon = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
    session = await _session_started_at(account, noon)
    base = _ms(noon)
    await ingest_location(
        session.id,
        [{"lat": 51.0 + i / 10, "lon": -114.0, "t": base + i * 1_000} for i in range(10)],
    )

    response = await client.get(
        "/v1/resources/location/tracks",
        params={"from_ms": base, "to_ms": base + 3_600_000, "max_points": 4},
        headers=account.headers,
    )
    (track,) = response.json()
    # Stride ceil(10/4)=3 keeps fixes 0,3,6,9 — the last fix survives.
    assert [p["at_ms"] for p in track["points"]] == [0, 3_000, 6_000, 9_000]
    assert track["point_count"] == 10
    # Bounds span every fix, not just the survivors.
    assert (track["min_lat"], track["max_lat"]) == (51.0, 51.9)


async def test_tracks_window_filters(
    client: httpx.AsyncClient, account: Account
) -> None:
    noon = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
    inside = await _session_started_at(account, noon)
    outside = await _session_started_at(account, datetime(2026, 8, 14, 18, 0, tzinfo=UTC))
    base = _ms(noon)
    await ingest_location(
        inside.id,
        [{"lat": 51.0 + i, "lon": -114.0, "t": base + i * 60_000} for i in range(5)],
    )
    await ingest_location(
        outside.id, [{"lat": 60.0, "lon": -100.0, "t": base + 6 * 3_600_000}]
    )

    response = await client.get(
        "/v1/resources/location/tracks",
        params={"from_ms": base + 60_000, "to_ms": base + 180_000},
        headers=account.headers,
    )
    (track,) = response.json()
    assert track["session_id"] == str(inside.id)
    assert [p["lat"] for p in track["points"]] == [52.0, 53.0]
    # Count and bounds are exact for the window, not the whole session.
    assert track["point_count"] == 2
    assert (track["min_lat"], track["max_lat"]) == (52.0, 53.0)


async def test_tracks_scoped_to_owner(
    client: httpx.AsyncClient, account: Account, other_account: Account
) -> None:
    noon = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
    theirs = await _session_started_at(other_account, noon)
    await ingest_location(theirs.id, [{"lat": 0.0, "lon": 0.0, "t": _ms(noon) + 1_000}])
    # A session of our own with no location data must not appear either.
    await make_session(account)

    response = await client.get(
        "/v1/resources/location/tracks",
        params={"from_ms": _ms(noon), "to_ms": _ms(noon) + 3_600_000},
        headers=account.headers,
    )
    assert response.status_code == 200
    assert response.json() == []


async def test_tracks_window_required_and_capped(
    client: httpx.AsyncClient, account: Account
) -> None:
    """The tracks query has no LIMIT, so the window is its only cost bound:
    it must be present and at most 92 days wide."""
    windowless = await client.get(
        "/v1/resources/location/tracks", headers=account.headers
    )
    assert windowless.status_code == 422

    base = _ms(datetime(2026, 1, 1, tzinfo=UTC))
    day = 24 * 3_600_000
    too_wide = await client.get(
        "/v1/resources/location/tracks",
        params={"from_ms": base, "to_ms": base + 93 * day},
        headers=account.headers,
    )
    assert too_wide.status_code == 422

    at_cap = await client.get(
        "/v1/resources/location/tracks",
        params={"from_ms": base, "to_ms": base + 92 * day},
        headers=account.headers,
    )
    assert at_cap.status_code == 200
