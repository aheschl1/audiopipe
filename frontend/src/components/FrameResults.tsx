import type { Schemas } from "../api/client"
import { fmtClock, shortId } from "../format"
import { artifactUrl } from "../visual"

type FrameSearchRead = Schemas["FrameSearchRead"]

/** Keyframe hits as a grid of stills; a click opens the session at that moment. */
export default function FrameResults({
  hits,
  onOpen,
}: {
  hits: FrameSearchRead[]
  onOpen: (sessionId: string, seekMs?: number) => void
}) {
  return (
    <div className="frame-grid">
      {hits.map((hit, rank) => (
        <button
          key={hit.artifact_id}
          className="frame-hit"
          onClick={() => onOpen(hit.session_id, hit.at_ms ?? undefined)}
          title={`#${rank + 1} · ${hit.reason} · distance ${hit.distance.toFixed(3)}`}
        >
          <span className="frame-img" style={{ aspectRatio: `${hit.width} / ${hit.height}` }}>
            <img src={artifactUrl(hit.artifact_id)} alt="" loading="lazy" />
          </span>
          <span className="frame-meta">
            {hit.at_ms !== null && hit.at_ms !== undefined ? fmtClock(hit.at_ms) : "Unplaced"}
            <span className="row-dim"> in {shortId(hit.session_id)}</span>
          </span>
        </button>
      ))}
    </div>
  )
}
