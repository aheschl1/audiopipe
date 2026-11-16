// Speaker presentation helpers shared by the speakers page, the session
// timeline, and the visual strip — one place so a cluster wears the same
// colour and label everywhere.

import { shortId } from "./format"
import { token } from "./styles/tokens"

export function initials(name: string | null | undefined, id: string): string {
  if (!name) return id.slice(0, 2)
  const parts = name.trim().split(/\s+/)
  return ((parts[0]?.[0] ?? "") + (parts[1]?.[0] ?? "")).toUpperCase() || id.slice(0, 2)
}

/** Slots in the categorical palette (--cat-1 … --cat-N in tokens.css). */
export const CATEGORY_COUNT = 8

/** Deterministic palette slot, so a cluster (or sound class) wears one colour everywhere. */
export function categoryIndex(id: string): number {
  let h = 0
  for (const ch of id) h = (h * 31 + ch.charCodeAt(0)) % 1000003
  return (h % CATEGORY_COUNT) + 1
}

/** The category colour as a CSS value, for inline styles. */
export function categoryColor(id: string): string {
  return `var(--cat-${categoryIndex(id)})`
}

/** The category colour at `percent` opacity, as a CSS value. */
export function categoryTint(id: string, percent: number): string {
  return `color-mix(in srgb, var(--cat-${categoryIndex(id)}) ${Math.round(percent)}%, transparent)`
}

/** The resolved hex, for Plotly and MapLibre. */
export function categoryHex(id: string): string {
  return token(`--cat-${categoryIndex(id)}`)
}

export function speakerLabel(speaker: { name?: string | null; id: string }): string {
  return speaker.name ?? shortId(speaker.id)
}

// Block-local diarization labels are namespaced (`b109176:SPEAKER_00`);
// only the tail is human-meaningful.
export function localLabelTail(label: string): string {
  return label.split(":").pop() ?? label
}
