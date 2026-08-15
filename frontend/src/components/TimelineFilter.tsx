import { useEffect, useRef, useState } from "react"

// Which timeline event kinds are shown. "marker" covers session-start/end.
export type EventKind = "transcript" | "sound-span" | "audio-tag" | "location-point" | "marker"

export const ALL_KINDS: { key: EventKind; label: string }[] = [
  { key: "transcript", label: "Transcripts" },
  { key: "sound-span", label: "Sound spans" },
  { key: "audio-tag", label: "Sound tags" },
  { key: "location-point", label: "Location" },
  { key: "marker", label: "Session markers" },
]

const STORAGE_KEY = "audiopipeline.timeline.hidden"

export function loadHidden(): Set<EventKind> {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return new Set()
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return new Set()
    const valid = new Set(ALL_KINDS.map((k) => k.key as string))
    return new Set(parsed.filter((k): k is EventKind => typeof k === "string" && valid.has(k)))
  } catch {
    return new Set()
  }
}

function saveHidden(hidden: Set<EventKind>): void {
  localStorage.setItem(STORAGE_KEY, JSON.stringify([...hidden]))
}

// A small checkbox dropdown for deselecting timeline item types; the choice
// persists across sessions and reloads.
export default function TimelineFilter({
  hidden,
  onChange,
}: {
  hidden: Set<EventKind>
  onChange: (hidden: Set<EventKind>) => void
}) {
  const [open, setOpen] = useState(false)
  const root = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (!open) return
    const onOutside = (event: MouseEvent) => {
      if (root.current && !root.current.contains(event.target as Node)) setOpen(false)
    }
    const onEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false)
    }
    document.addEventListener("mousedown", onOutside)
    document.addEventListener("keydown", onEscape)
    return () => {
      document.removeEventListener("mousedown", onOutside)
      document.removeEventListener("keydown", onEscape)
    }
  }, [open])

  const toggle = (kind: EventKind) => {
    const next = new Set(hidden)
    if (next.has(kind)) next.delete(kind)
    else next.add(kind)
    saveHidden(next)
    onChange(next)
  }

  return (
    <div className="filter-menu" ref={root}>
      <button className={`btn ghost slim ${hidden.size > 0 ? "filtered" : ""}`} onClick={() => setOpen((o) => !o)}>
        {hidden.size > 0 ? `showing ${ALL_KINDS.length - hidden.size}/${ALL_KINDS.length}` : "filter"} ▾
      </button>
      {open && (
        <div className="filter-pop">
          {ALL_KINDS.map(({ key, label }) => (
            <label key={key} className="filter-option">
              <input
                type="checkbox"
                checked={!hidden.has(key)}
                onChange={() => toggle(key)}
              />
              {label}
            </label>
          ))}
        </div>
      )}
    </div>
  )
}
