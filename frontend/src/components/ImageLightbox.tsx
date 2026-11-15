import { useEffect } from "react"
import type { ImageEvent } from "../api/client"
import { fmtClock } from "../format"
import { fmtWallTime, imageFullUrl } from "../visual"

/** Full-size view of one image, with prev/next across the session's images. */
export default function ImageLightbox({
  images,
  index,
  onIndex,
  onClose,
  onSeek,
}: {
  images: ImageEvent[]
  index: number
  onIndex: (index: number) => void
  onClose: () => void
  onSeek: (ms: number) => void
}) {
  const image = images[index]

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose()
      else if (e.key === "ArrowLeft" && index > 0) onIndex(index - 1)
      else if (e.key === "ArrowRight" && index < images.length - 1) onIndex(index + 1)
    }
    document.addEventListener("keydown", onKey)
    return () => document.removeEventListener("keydown", onKey)
  }, [index, images.length, onIndex, onClose])

  if (!image) return null
  const at = Math.max(0, image.at_ms)

  return (
    <div className="lightbox" onClick={onClose}>
      <div className="lightbox-frame" onClick={(e) => e.stopPropagation()}>
        <img
          className="lightbox-img"
          src={imageFullUrl(image)}
          alt={`Image at ${fmtClock(at)}`}
        />
        <div className="lightbox-bar">
          <button
            className="btn ghost slim"
            disabled={index === 0}
            onClick={() => onIndex(index - 1)}
            title="Previous (←)"
          >
            ‹
          </button>
          <span className="row-dim">
            {index + 1}/{images.length} · {fmtWallTime(image.captured_at)} ·{" "}
            {fmtClock(at)}
            {image.width && image.height ? ` · ${image.width}×${image.height}` : ""}
          </span>
          <button
            className="btn ghost slim"
            onClick={() => {
              onSeek(at)
              onClose()
            }}
          >
            Listen from here
          </button>
          <button
            className="btn ghost slim"
            disabled={index === images.length - 1}
            onClick={() => onIndex(index + 1)}
            title="Next (→)"
          >
            ›
          </button>
          <button className="btn ghost slim" onClick={onClose} title="Close (Esc)">
            ✕
          </button>
        </div>
      </div>
    </div>
  )
}
