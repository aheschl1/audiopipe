/** An on/off switch; `onToggle` absent renders it read-only. */
export default function Switch({
  on,
  label,
  onToggle,
}: {
  on: boolean
  label: string
  onToggle?: () => void
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={label}
      className={`switch ${on ? "on" : ""}`}
      disabled={!onToggle}
      onClick={(e) => {
        e.stopPropagation()
        onToggle?.()
      }}
    >
      <span className="switch-knob" />
      <span className="switch-text">{on ? "On" : "Off"}</span>
    </button>
  )
}
