import { useEffect, useState } from "react"
import { onRefused } from "../readOnly"

/** A brief, quiet line when the API refuses a demo write. */
export default function ReadOnlyNotice() {
  const [shown, setShown] = useState(0)

  useEffect(() => onRefused(() => setShown((n) => n + 1)), [])
  useEffect(() => {
    if (!shown) return
    const timer = window.setTimeout(() => setShown(0), 2600)
    return () => window.clearTimeout(timer)
  }, [shown])

  return (
    <div className={`ro-notice ${shown ? "on" : ""}`} role="status" aria-hidden={!shown}>
      Read-only demo. That change wasn't saved.
    </div>
  )
}
