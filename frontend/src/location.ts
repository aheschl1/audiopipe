// Location helpers, mirroring sounds.ts: one place for the lane's data and
// coordinate formatting so points read the same everywhere.

import type { LocationPointEvent, TimelineEvent } from "./api/client"

export function locationPoints(events: TimelineEvent[]): LocationPointEvent[] {
  return events.filter((e): e is LocationPointEvent => e.type === "location-point")
}

// 5 decimals ≈ 1 m — as precise as consumer GPS gets.
export function fmtCoords(point: { lat: number; lon: number }): string {
  return `${point.lat.toFixed(5)}, ${point.lon.toFixed(5)}`
}

export function describePoint(point: LocationPointEvent): string {
  const parts = [fmtCoords(point)]
  if (point.alt_m != null) parts.push(`${Math.round(point.alt_m)} m alt`)
  if (point.accuracy_m != null) parts.push(`±${Math.round(point.accuracy_m)} m`)
  return parts.join(" · ")
}

// Fixes further apart than this are treated as separate track segments: the
// device was off (or indoors) and a straight line between them is fiction.
export const TRACK_GAP_MS = 5 * 60_000

type TimedPoint = { at_ms: number; lat: number; lon: number }

export type TrackPosition = {
  lat: number
  lon: number
  // False when the position is a guess: clamped to an endpoint or snapped
  // across a gap rather than interpolated between two nearby fixes.
  exact: boolean
}

// Where was the device at `ms` on the session timeline? Points must be sorted
// by at_ms (both the timeline stream and the points endpoint guarantee it).
export function positionAtMs(
  points: readonly TimedPoint[],
  ms: number,
): TrackPosition | null {
  const first = points[0]
  const last = points[points.length - 1]
  if (!first || !last) return null
  if (ms <= first.at_ms) return { lat: first.lat, lon: first.lon, exact: ms === first.at_ms }
  if (ms >= last.at_ms) return { lat: last.lat, lon: last.lon, exact: ms === last.at_ms }
  let lo = 0
  let hi = points.length - 1
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1
    if (points[mid]!.at_ms <= ms) lo = mid
    else hi = mid
  }
  const a = points[lo]!
  const b = points[hi]!
  if (b.at_ms - a.at_ms > TRACK_GAP_MS) {
    const nearest = ms - a.at_ms <= b.at_ms - ms ? a : b
    return { lat: nearest.lat, lon: nearest.lon, exact: false }
  }
  const f = b.at_ms === a.at_ms ? 0 : (ms - a.at_ms) / (b.at_ms - a.at_ms)
  return {
    lat: a.lat + (b.lat - a.lat) * f,
    lon: a.lon + (b.lon - a.lon) * f,
    exact: true,
  }
}

// Split a sorted track into contiguous segments at capture gaps, so a map
// draws separate polylines instead of a line across the off period.
export function splitAtGaps<T extends { at_ms: number }>(
  points: readonly T[],
  gapMs: number = TRACK_GAP_MS,
): T[][] {
  const segments: T[][] = []
  let current: T[] = []
  for (const point of points) {
    if (current.length > 0 && point.at_ms - current[current.length - 1]!.at_ms > gapMs) {
      segments.push(current)
      current = []
    }
    current.push(point)
  }
  if (current.length > 0) segments.push(current)
  return segments
}
