import { useEffect, useMemo, useRef, useState } from "react"
import { api, type LocationPointRead, type TimelineEvent } from "../api/client"
import { locationPoints } from "../location"

const PAGE = 200
const MAX_POINTS = 5000

// Stable empty array so consumers' effect deps don't churn between renders.
const NO_POINTS: never[] = []

// A session's fixes are immutable, and the panel unmounts on hide — cache
// completed fetches so toggling the map chip doesn't repeat the paging.
const trackCache = new Map<string, { points: LocationPointRead[]; partial: boolean }>()
const CACHE_MAX = 8

// The structural subset both LocationPointEvent and LocationPointRead carry —
// everything the map needs.
export type TrackFix = {
  at_ms: number
  lat: number
  lon: number
}

export type SessionTrack = {
  points: TrackFix[]
  partial: boolean
  loading: boolean
  failed: boolean
}

// A session's fixes for the map panel. The timeline events already in memory
// are the free path; only a truncated timeline (5000-event cap) falls back to
// paging the points endpoint, and only while the panel is actually mounted.
// `points` is referentially stable across unrelated re-renders (the playhead
// ticks the parent ~4x/s), so map effects keyed on it only fire on real data.
export function useSessionTrack(
  sessionId: string,
  events: TimelineEvent[] | null,
  truncated: boolean,
): SessionTrack {
  const [fetched, setFetched] = useState<{
    points: LocationPointRead[]
    partial: boolean
    failed: boolean
  } | null>(null)
  const generation = useRef(0)

  const eventPoints = useMemo(
    () => (events ? locationPoints(events) : NO_POINTS),
    [events],
  )

  useEffect(() => {
    if (!truncated) return
    const gen = ++generation.current
    const cached = trackCache.get(sessionId)
    if (cached) {
      setFetched({ ...cached, failed: false })
      return
    }
    setFetched(null)
    void (async () => {
      const all: LocationPointRead[] = []
      let partial = false
      let failed = false
      try {
        for (;;) {
          const { data } = await api.GET(
            "/v1/sessions/{session_id}/resources/location/points",
            {
              params: {
                path: { session_id: sessionId },
                query: { limit: PAGE, offset: all.length },
              },
            },
          )
          if (generation.current !== gen) return
          if (!data) {
            // An HTTP error page is not end-of-data: keep what we have, but
            // surface it instead of presenting the prefix as complete.
            failed = true
            break
          }
          all.push(...data.items)
          if (!data.has_more) break
          if (all.length >= MAX_POINTS) {
            partial = true
            break
          }
        }
      } catch {
        failed = true
      }
      if (!failed) {
        trackCache.set(sessionId, { points: all, partial })
        if (trackCache.size > CACHE_MAX)
          trackCache.delete(trackCache.keys().next().value!)
      }
      if (generation.current === gen) setFetched({ points: all, partial, failed })
    })()
    return () => {
      generation.current++
    }
  }, [sessionId, truncated])

  if (!truncated) {
    return { points: eventPoints, partial: false, loading: !events, failed: false }
  }
  return {
    points: fetched?.points ?? NO_POINTS,
    partial: fetched?.partial ?? false,
    loading: fetched === null,
    failed: fetched?.failed ?? false,
  }
}
