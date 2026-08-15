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
