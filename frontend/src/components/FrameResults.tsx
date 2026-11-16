import type { Schemas } from "../api/client"
import { artifactUrl } from "../visual"

type FrameSearchRead = Schemas["FrameSearchRead"]

/** Keyframe hits as a grid of stills; a click opens its moment. */
export default function FrameResults({
  hits,
  onOpen,
  stamp,
}: {
  hits: FrameSearchRead[]
  onOpen: (sessionId: string, seekMs?: number) => void
  stamp: (sessionId: string, ms: number | null | undefined) => string | null
}) {
  return (
    <div className="frame-grid">
      {hits.map((hit) => (
        <button
          key={hit.artifact_id}
          className="frame-hit"
          onClick={() => onOpen(hit.session_id, hit.at_ms ?? undefined)}
          title={hit.reason}
        >
          <span className="frame-img" style={{ aspectRatio: `${hit.width} / ${hit.height}` }}>
            <img src={artifactUrl(hit.artifact_id)} alt="" loading="lazy" />
          </span>
          <span className="frame-meta">
            {stamp(hit.session_id, hit.at_ms)}
          </span>
        </button>
      ))}
    </div>
  )
}
