// Time formatting shared by the timeline, player, and search results.

export function fmtClock(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000))
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  const mm = h > 0 ? String(m).padStart(2, "0") : String(m)
  return `${h > 0 ? `${h}:` : ""}${mm}:${String(s).padStart(2, "0")}`
}

export function fmtDate(iso: string): string {
  return new Date(iso).toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  })
}

export function fmtDuration(startIso: string, endIso: string | null): string {
  if (!endIso) return "live"
  return fmtClock(new Date(endIso).getTime() - new Date(startIso).getTime())
}

export function shortId(id: string): string {
  return id.slice(0, 8)
}

export function dayKey(iso: string): string {
  const d = new Date(iso)
  return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`
}

/** "Today", "Yesterday", or a weekday date, for day headings. */
export function dayLabel(iso: string): string {
  const d = new Date(iso)
  const today = new Date()
  const yesterday = new Date()
  yesterday.setDate(today.getDate() - 1)
  if (dayKey(iso) === dayKey(today.toISOString())) return "Today"
  if (dayKey(iso) === dayKey(yesterday.toISOString())) return "Yesterday"
  return d.toLocaleDateString([], {
    weekday: "short",
    month: "short",
    day: "numeric",
    year: d.getFullYear() === today.getFullYear() ? undefined : "numeric",
  })
}

/** "Sunday, November 15" (with the year when it isn't this one). */
export function fmtLongDay(iso: string): string {
  const d = new Date(iso)
  return d.toLocaleDateString([], {
    weekday: "long",
    month: "long",
    day: "numeric",
    year: d.getFullYear() === new Date().getFullYear() ? undefined : "numeric",
  })
}

export function timeOfDay(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
}

/** A wall-clock span in words: "8 min", "1 h 12 min". */
export function fmtSpan(startIso: string, endIso: string): string {
  const minutes = Math.max(1, Math.round((Date.parse(endIso) - Date.parse(startIso)) / 60_000))
  if (minutes < 60) return `${minutes} min`
  const h = Math.floor(minutes / 60)
  const m = minutes % 60
  return m ? `${h} h ${m} min` : `${h} h`
}

/** A compact wall clock, "5:40 PM" or "17:40" by locale. */
export function clock(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
}
