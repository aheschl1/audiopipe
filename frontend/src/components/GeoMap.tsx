import { useEffect, useMemo, useRef, useState } from "react"
import { api, type SessionTrackRead } from "../api/client"
import { fmtDate, shortId } from "../format"
import { fmtCoords, splitAtGaps, TRACK_GAP_MS } from "../location"
import { createTrackMap, type TrackMapHandle, type TrackPt } from "../map/trackMap"
import { hue } from "../speakers"

const MAX_POINTS = 500

// The backend caps the tracks window at 92 days; a [from, to] pair spans
// to - from + 1 days, so the pickers keep the dates within 91 of each other.
const MAX_RANGE_DAYS = 91

function isoDate(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0")
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

function shiftDays(day: string, delta: number): string {
  const d = new Date(`${day}T00:00`)
  d.setDate(d.getDate() + delta)
  return isoDate(d)
}

function defaultRange(): { from: string; to: string } {
  const now = new Date()
  const week = new Date(now)
  week.setDate(week.getDate() - 6)
  return { from: isoDate(week), to: isoDate(now) }
}

// Local-midnight epoch window, half-open on the day after `to`. Day math via
// setDate, never +86_400_000 — DST days aren't 24h.
function epochWindow(from: string, to: string): { from_ms: number; to_ms: number } {
  const start = new Date(`${from}T00:00`)
  const end = new Date(`${to}T00:00`)
  end.setDate(end.getDate() + 1)
  return { from_ms: start.getTime(), to_ms: end.getTime() }
}

function trackName(track: SessionTrackRead): string {
  return track.label ?? track.device ?? shortId(track.session_id)
}

function trackColor(track: SessionTrackRead): string {
  return `hsl(${hue(track.session_id)} 60% 55%)`
}

// The global travel map: every session's track in a date window, one color
// per session, with click-through into the session at the clicked fix.
export default function GeoMap({
  onOpen,
}: {
  onOpen: (sessionId: string, seekMs?: number) => void
}) {
  const [range, setRange] = useState(defaultRange)
  const [tracks, setTracks] = useState<SessionTrackRead[] | null>(null)
  const [error, setError] = useState(false)
  const [selected, setSelected] = useState<{ sessionId: string; pt: TrackPt } | null>(null)
  const containerRef = useRef<HTMLDivElement | null>(null)
  const [handle, setHandle] = useState<TrackMapHandle | null>(null)
  const [mapFailed, setMapFailed] = useState(false)

  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    let dead = false
    let created: TrackMapHandle | null = null
    void createTrackMap(el, {
      onPointClick: (trackId, pt) => {
        const sessionId = trackId.split(":")[0] ?? trackId
        setSelected({ sessionId, pt })
      },
    })
      .then((map) => {
        if (dead) {
          map.destroy()
          return
        }
        created = map
        setHandle(map)
      })
      .catch(() => {
        if (!dead) setMapFailed(true)
      })
    return () => {
      dead = true
      created?.destroy()
      setHandle(null)
    }
  }, [])

  useEffect(() => {
    let stale = false
    setError(false)
    setSelected(null)
    // Drop the old range's tracks up front so a failed reload can't leave
    // them on the map under the new dates.
    setTracks(null)
    void api
      .GET("/v1/resources/location/tracks", {
        params: { query: { ...epochWindow(range.from, range.to), max_points: MAX_POINTS } },
      })
      .then(({ data }) => {
        if (stale) return
        if (data) setTracks(data)
        else setError(true)
      })
      .catch(() => {
        if (!stale) setError(true)
      })
    return () => {
      stale = true
    }
  }, [range])

  useEffect(() => {
    if (!handle || !tracks) return
    handle.setTracks(
      tracks.flatMap((track) => {
        // The gap threshold is calibrated to raw capture spacing; decimation
        // multiplies spacing by the stride, so scale it or a long gap-free
        // track degrades into unconnected single-point segments.
        const stride = Math.max(1, Math.ceil(track.point_count / MAX_POINTS))
        return splitAtGaps(track.points, TRACK_GAP_MS * stride).map((segment, i) => ({
          id: `${track.session_id}:${i}`,
          color: trackColor(track),
          label: trackName(track),
          points: segment.map((p) => ({ atMs: p.at_ms, lat: p.lat, lon: p.lon })),
        }))
      }),
    )
    handle.fitTracks()
  }, [handle, tracks])

  const totalFixes = useMemo(
    () => (tracks ?? []).reduce((sum, t) => sum + t.point_count, 0),
    [tracks],
  )
  const selectedTrack = selected
    ? (tracks ?? []).find((t) => t.session_id === selected.sessionId)
    : undefined

  return (
    <div className="geo-view">
      <div className="geo-toolbar">
        <label className="geo-range">
          from
          <input
            type="date"
            className="input slim"
            value={range.from}
            min={shiftDays(range.to, -MAX_RANGE_DAYS)}
            max={range.to}
            onChange={(e) => e.target.value && setRange((r) => ({ ...r, from: e.target.value }))}
          />
        </label>
        <label className="geo-range">
          to
          <input
            type="date"
            className="input slim"
            value={range.to}
            min={range.from}
            max={shiftDays(range.from, MAX_RANGE_DAYS)}
            onChange={(e) => e.target.value && setRange((r) => ({ ...r, to: e.target.value }))}
          />
        </label>
        {tracks && tracks.length > 0 && (
          <span className="row-dim">
            {tracks.length} session{tracks.length === 1 ? "" : "s"} · {totalFixes} fixes
          </span>
        )}
      </div>
      <div className="geo-body">
        <div className="geo-map-wrap">
          <div ref={containerRef} className="geo-map" />
          {tracks && tracks.length === 0 && (
            <div className="geo-empty">no location data between these dates</div>
          )}
          {error && <div className="geo-empty">couldn't load tracks</div>}
          {mapFailed && <div className="geo-empty">couldn't load the map</div>}
        </div>
        <aside className="geo-legend">
          {selected && selectedTrack && (
            <div className="geo-point-card">
              <div className="geo-point-head">
                <strong>{trackName(selectedTrack)}</strong>
                <button className="btn ghost slim" onClick={() => setSelected(null)}>
                  ✕
                </button>
              </div>
              <div className="row-dim">{fmtCoords(selected.pt)}</div>
              <div className="row-dim">
                {fmtDate(new Date(
                  new Date(selectedTrack.started_at).getTime() + selected.pt.atMs,
                ).toISOString())}
              </div>
              <button
                className="btn primary slim"
                onClick={() => onOpen(selected.sessionId, Math.max(0, selected.pt.atMs))}
              >
                open session here
              </button>
            </div>
          )}
          {(tracks ?? []).map((track) => (
            <div
              key={track.session_id}
              className="geo-session"
              onClick={() =>
                handle?.fitBounds({
                  minLat: track.min_lat,
                  maxLat: track.max_lat,
                  minLon: track.min_lon,
                  maxLon: track.max_lon,
                })
              }
            >
              <span className="geo-swatch" style={{ background: trackColor(track) }} />
              <div className="geo-session-info">
                <div className="geo-session-name">{trackName(track)}</div>
                <div className="row-dim">
                  {fmtDate(track.started_at)} · {track.point_count} fixes
                </div>
              </div>
              <button
                className="btn ghost slim"
                onClick={(e) => {
                  e.stopPropagation()
                  onOpen(track.session_id)
                }}
              >
                open
              </button>
            </div>
          ))}
        </aside>
      </div>
    </div>
  )
}
