import { toggleClip, useClipPlayer, type Clip } from "../clipPlayer"

// Inline play/stop for one clip. Sits inside clickable rows, so it swallows
// the click that would otherwise navigate.
export default function ClipButton({ clip }: { clip: Clip }) {
  const player = useClipPlayer()
  const active = player.key === clip.key

  return (
    <button
      className={`clip-btn ${active ? "active" : ""}`}
      title={active ? "Stop" : "Play here"}
      onClick={(event) => {
        event.stopPropagation()
        void toggleClip(clip)
      }}
    >
      {active ? (player.status === "loading" ? "·" : "◼") : "▶"}
    </button>
  )
}

// Thin progress line rendered at the bottom of the row that owns the
// playing clip.
export function ClipProgress({ clip }: { clip: Clip }) {
  const player = useClipPlayer()
  if (player.key !== clip.key || player.status !== "playing") return null
  const fraction = Math.min(1, player.progressMs / Math.max(1, clip.endMs - clip.startMs))
  return <span className="clip-progress" style={{ width: `${fraction * 100}%` }} />
}
