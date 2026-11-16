// Sound-class lanes, mirroring speakers.ts for voices: one place so a class
// wears the same colour and ordering everywhere.

import type { SoundSpanEvent, TimelineEvent } from "./api/client"
import { fmtClock } from "./format"

// Lanes shown before the "+N more" toggle, and a ceiling in case the tier's
// own cap is ever mis-tuned.
export const SOUND_LANES_SHOWN = 5
const MAX_SOUND_LANES = 24

// Sound classes scoring below this are noise (a microwave in a service bay): hidden everywhere.
export const MIN_SOUND_SCORE = 0.53

export const audible = (score: number | null | undefined) => (score ?? 1) >= MIN_SOUND_SCORE

export type SoundLane = {
  label: string
  coveredMs: number
  peak: number
  spans: SoundSpanEvent[]
}

// One lane per class. Same-class spans never overlap (the tier's invariant),
// so a lane is a valid horizontal timeline with no packing; different classes
// overlap freely, which is why they get rows of their own.
export function buildSoundLanes(events: TimelineEvent[]): SoundLane[] {
  const lanes = new Map<string, SoundLane>()
  for (const event of events) {
    if (event.type !== "sound-span" || !audible(event.peak)) continue
    let lane = lanes.get(event.label)
    if (!lane) {
      lane = { label: event.label, coveredMs: 0, peak: 0, spans: [] }
      lanes.set(event.label, lane)
    }
    lane.coveredMs += Math.max(0, event.end_ms - event.start_ms)
    lane.peak = Math.max(lane.peak, event.peak ?? 0)
    lane.spans.push(event)
  }
  // Most confident on top; covered time only breaks ties.
  return [...lanes.values()]
    .sort(
      (a, b) =>
        b.peak - a.peak || b.coveredMs - a.coveredMs || a.label.localeCompare(b.label),
    )
    .slice(0, MAX_SOUND_LANES)
}

// Confidence as opacity, per span: `mean` is how solidly that class held over
// that stretch, so a weakly-held span reads as a wash and a solid one as
// solid. Normalised from the tier's sustain floor rather than from 0 — no
// published span scores below it, so mapping [0,1] would waste most of the
// range and flatten the contrast.
const FAINTEST = 0.15
const STRONGEST = 0.9
const FLOOR = 0.2

export function soundAlpha(span: SoundSpanEvent): number {
  return FAINTEST + (STRONGEST - FAINTEST) * confidence(span)
}

// The border follows the fill but from a higher floor, so a faint span still
// reads as a clickable block rather than dissolving into the lane.
export function soundBorderAlpha(span: SoundSpanEvent): number {
  return 0.45 + 0.5 * confidence(span)
}

function confidence(span: SoundSpanEvent): number {
  const score = span.mean ?? span.peak ?? 0.5
  return Math.min(1, Math.max(0, (score - FLOOR) / (1 - FLOOR)))
}

export function describeSpan(span: SoundSpanEvent): string {
  const parts = [`${fmtClock(span.end_ms - span.start_ms)} long`]
  if (span.peak != null) parts.push(`peak ${span.peak.toFixed(2)}`)
  if (span.mean != null) parts.push(`avg ${span.mean.toFixed(2)}`)
  if (span.windows) parts.push(`${span.windows} window${span.windows === 1 ? "" : "s"}`)
  return parts.join(" · ")
}
