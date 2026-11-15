import type { ProjectionPointRead } from "../api/client"
import { fmtClock, fmtDate, shortId } from "../format"
import { categoryColor, localLabelTail } from "../speakers"
import ClipButton, { ClipProgress } from "./ClipButton"

// Distances past this read as "probably a different voice" — the same
// threshold SpeakersView warns on.
const FAR_DISTANCE = 0.85

export default function SpeakerMapInspector({
  point,
  onOpen,
  onClose,
}: {
  point: ProjectionPointRead
  onOpen: (sessionId: string, seekMs?: number) => void
  onClose: () => void
}) {
  const tint = categoryColor(point.speaker_id ?? point.speaker)
  const clip =
    point.clip_start_ms !== null &&
    point.clip_start_ms !== undefined &&
    point.clip_end_ms !== null &&
    point.clip_end_ms !== undefined
      ? {
          key: `map:${point.artifact_id}`,
          sessionId: point.session_id,
          startMs: point.clip_start_ms,
          endMs: point.clip_end_ms,
        }
      : null

  return (
    <aside className="map-inspector">
      <div className="map-inspector-head">
        <span className="map-swatch" style={{ background: tint }} />
        <strong>{point.name ?? (point.speaker_id ? shortId(point.speaker_id) : "unassigned")}</strong>
        <button className="btn ghost slim" onClick={onClose}>
          ✕
        </button>
      </div>

      <dl className="map-facts">
        <dt>session</dt>
        <dd>{point.session_label ?? shortId(point.session_id)}</dd>
        <dt>recorded</dt>
        <dd>{fmtDate(point.started_at)}</dd>
        {point.start_ms !== null && point.start_ms !== undefined && (
          <>
            <dt>block</dt>
            <dd>
              {fmtClock(point.start_ms)}–{fmtClock(point.end_ms ?? point.start_ms)}
            </dd>
          </>
        )}
        <dt>label</dt>
        <dd className="mono">{localLabelTail(point.speaker)}</dd>
        {point.talk_ms !== null && point.talk_ms !== undefined && (
          <>
            <dt>speech</dt>
            <dd>{fmtClock(point.talk_ms)}</dd>
          </>
        )}
        <dt>distance</dt>
        <dd>
          {point.distance === null || point.distance === undefined ? (
            <span className="row-dim">unassigned</span>
          ) : (
            <span className={`distance ${point.distance > FAR_DISTANCE ? "far" : ""}`}>
              {point.distance.toFixed(3)}
            </span>
          )}
        </dd>
        {point.split_of && (
          <>
            <dt>split from</dt>
            <dd className="mono">{point.split_of}</dd>
          </>
        )}
      </dl>

      {point.pinned && <span className="chip">📌 pinned</span>}

      <div className="map-inspector-actions">
        {clip && (
          <span className="map-clip">
            <ClipButton clip={clip} />
            <ClipProgress clip={clip} />
          </span>
        )}
        <button
          className="btn slim"
          onClick={() => onOpen(point.session_id, point.start_ms ?? 0)}
        >
          open in session
        </button>
      </div>

      <p className="map-note">
        Distance is measured in the full embedding space, not on screen.
      </p>
    </aside>
  )
}
