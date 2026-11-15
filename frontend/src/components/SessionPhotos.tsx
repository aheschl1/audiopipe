import type { ImageEvent } from "../api/client"
import { fmtClock } from "../format"
import { imageThumbUrl } from "../visual"

/** Every photo in the session as a contact sheet; opens the lightbox at that photo. */
export default function SessionPhotos({
  images,
  token,
  onOpen,
}: {
  images: ImageEvent[]
  token: string
  onOpen: (index: number) => void
}) {
  return (
    <section className="aside-section session-photos">
      <h2 className="section-label">
        Photos <span className="section-count">{images.length}</span>
      </h2>
      <div className="contact-sheet">
        {images.map((image, index) => (
          <button
            key={image.segment_id}
            className="contact"
            title={`photo at ${fmtClock(Math.max(0, image.at_ms))}`}
            onClick={() => onOpen(index)}
          >
            <img src={imageThumbUrl(image, token)} alt="" loading="lazy" />
            <span className="contact-time">{fmtClock(Math.max(0, image.at_ms))}</span>
          </button>
        ))}
      </div>
    </section>
  )
}
