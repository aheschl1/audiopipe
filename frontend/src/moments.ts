// Moment presentation helpers shared by the feed and the moment view.

import type { MomentSpeaker } from "./api/moments"

/** "Sam and Alex", "Sam, Alex and 2 others". */
export function joinNames(names: string[], max = 3): string {
  if (names.length <= 1) return names[0] ?? ""
  if (names.length > max) return `${names.slice(0, max - 1).join(", ")} and ${names.length - max + 1} others`
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`
}

/** Unnamed voices come back as "voice 1", "voice 2", … */
export function isUnnamed(speaker: MomentSpeaker): boolean {
  return /^voice \d+$/i.test(speaker.name)
}

/** Everyone heard: named people first, then a count of unnamed voices. */
export function heardWith(speakers: MomentSpeaker[], max = 3): string | null {
  const named = speakers.filter((s) => !isUnnamed(s)).map((s) => s.name)
  const unnamed = speakers.length - named.length
  const voices = unnamed ? `${unnamed} unnamed voice${unnamed === 1 ? "" : "s"}` : null
  if (!named.length) return voices && `with ${voices}`
  return `with ${voices ? joinNames([...named.slice(0, max - 1), voices], max + 1) : joinNames(named, max)}`
}

/** The People page entry for a clustered voice. */
export function personHref(speakerId: string): string {
  return `/speakers?speaker=${encodeURIComponent(speakerId)}`
}
