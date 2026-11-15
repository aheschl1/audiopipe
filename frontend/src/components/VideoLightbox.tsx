import { useEffect, useRef } from "react"
import type { VideoClipEvent } from "../api/client"
import { fmtClock } from "../format"
import { fmtWallTime } from "../visual"

/** Plays one clip full size, starting `offsetMs` into it. */
export default function VideoLightbox({
  clip,
  src,
  offsetMs,
  onClose,
  onSeek,
}: {
  clip: VideoClipEvent
  src: string
  offsetMs: number
  onClose: () => void
  onSeek: (ms: number) => void
}) {
  const ref = useRef<HTMLVideoElement | null>(null)

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose()
    }
    document.addEventListener("keydown", onKey)
    return () => document.removeEventListener("keydown", onKey)
  }, [onClose])

  const at = Math.max(0, clip.at_ms)
  return (
    <div className="lightbox" onClick={onClose}>
      <div className="lightbox-frame" onClick={(e) => e.stopPropagation()}>
        <video
          ref={ref}
          className="lightbox-img"
          src={src}
          controls
          autoPlay
          onLoadedMetadata={(e) => {
            e.currentTarget.currentTime = offsetMs / 1000
          }}
        />
        <div className="lightbox-bar">
          <span className="row-dim">
            {fmtWallTime(clip.captured_at)} · {fmtClock(at)}–{fmtClock(Math.max(0, clip.end_ms))}
          </span>
          <button
            className="btn ghost slim"
            onClick={() => {
              onSeek(at + Math.round((ref.current?.currentTime ?? 0) * 1000))
              onClose()
            }}
          >
            Listen from here
          </button>
          <button className="btn ghost slim" onClick={onClose} title="Close (Esc)">
            ✕
          </button>
        </div>
      </div>
    </div>
  )
}
