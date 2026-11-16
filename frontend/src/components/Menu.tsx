import { type ReactNode, useEffect, useRef, useState } from "react"
import { Link } from "react-router"

export type MenuItem =
  | { label: string; onSelect: () => void; danger?: boolean }
  | { label: string; to: string }

/** A "⋯" button that opens a short list of secondary actions. */
export default function Menu({
  items,
  label = "More",
  trigger,
}: {
  items: MenuItem[]
  label?: string
  trigger?: ReactNode
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

  return (
    <div className="filter-menu menu" ref={root} onClick={(e) => e.stopPropagation()}>
      <button
        type="button"
        className="btn ghost slim menu-trigger"
        aria-label={label}
        title={label}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        {trigger ?? "⋯"}
      </button>
      {open && (
        <div className="filter-pop menu-pop" role="menu">
          {items.map((item) =>
            "to" in item ? (
              <Link key={item.label} to={item.to} role="menuitem" className="menu-item" onClick={() => setOpen(false)}>
                {item.label}
              </Link>
            ) : (
              <button
                key={item.label}
                type="button"
                role="menuitem"
                className={`menu-item ${item.danger ? "danger" : ""}`}
                onClick={() => {
                  setOpen(false)
                  item.onSelect()
                }}
              >
                {item.label}
              </button>
            ),
          )}
        </div>
      )}
    </div>
  )
}
