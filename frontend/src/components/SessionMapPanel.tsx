import { useEffect, useRef, useState } from "react"
import type { TimelineEvent } from "../api/client"
import { usePlayhead } from "../hooks/usePlayhead"
import { useSessionTrack, type TrackFix } from "../hooks/useSessionTrack"
import { positionAtMs, splitAtGaps } from "../location"
import { createTrackMap, type TrackMapHandle } from "../map/trackMap"

// Matches the location lane's dot in the strip.
const TRACK_COLOR = "hsl(200 62% 58%)"

// The session's track, expandable under the strip. Owns its map instance;
// the playhead marker lives in a child so per-frame ticks never re-render
// the panel (same isolation move as PlayheadOverlay).
export default function SessionMapPanel({
  sessionId,
  events,
  truncated,
  audioEl,
  onSeek,
}: {
  sessionId: string
  events: TimelineEvent[] | null
  truncated: boolean
  audioEl: HTMLAudioElement | null
  onSeek: (ms: number, play: boolean) => void
}) {
  const { points, partial, loading } = useSessionTrack(sessionId, events, truncated)
  const containerRef = useRef<HTMLDivElement | null>(null)
  const [handle, setHandle] = useState<TrackMapHandle | null>(null)
  const onSeekRef = useRef(onSeek)
  onSeekRef.current = onSeek

  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    let dead = false
    let created: TrackMapHandle | null = null
    void createTrackMap(el, {
      onPointClick: (_track, pt) => onSeekRef.current(Math.max(0, pt.atMs), true),
    }).then((map) => {
      if (dead) {
        map.destroy()
        return
      }
      created = map
      setHandle(map)
    })
    return () => {
      dead = true
      created?.destroy()
      setHandle(null)
    }
  }, [])

  useEffect(() => {
    if (!handle) return
    handle.setTracks(
      splitAtGaps(points).map((segment, i) => ({
        id: `${sessionId}:${i}`,
        color: TRACK_COLOR,
        points: segment.map((p) => ({ atMs: p.at_ms, lat: p.lat, lon: p.lon })),
      })),
    )
    handle.fitTracks()
  }, [handle, points, sessionId])

  return (
    <div className="session-map">
      <div ref={containerRef} className="session-map-canvas" />
      {handle && <MapPlayheadMarker handle={handle} points={points} audioEl={audioEl} />}
      {loading && <div className="session-map-note">loading track…</div>}
      {partial && (
        <div className="session-map-note">long track — showing the first 5000 fixes</div>
      )}
    </div>
  )
}

function MapPlayheadMarker({
  handle,
  points,
  audioEl,
}: {
  handle: TrackMapHandle
  points: TrackFix[]
  audioEl: HTMLAudioElement | null
}) {
  const head = usePlayhead(audioEl, "smooth")
  useEffect(() => {
    handle.setMarker(positionAtMs(points, head.ms))
  }, [handle, points, head.ms])
  return null
}
