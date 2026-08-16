import { useEffect, useRef, useState } from "react"
import { api, type LocationPointRead, type TimelineEvent } from "../api/client"
import { locationPoints } from "../location"

const PAGE = 200
const MAX_POINTS = 5000

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
}

// A session's fixes for the map panel. The timeline events already in memory
// are the free path; only a truncated timeline (5000-event cap) falls back to
// paging the points endpoint, and only while the panel is actually mounted.
export function useSessionTrack(
  sessionId: string,
  events: TimelineEvent[] | null,
  truncated: boolean,
): SessionTrack {
  const [fetched, setFetched] = useState<{ points: LocationPointRead[]; partial: boolean } | null>(
    null,
  )
  const generation = useRef(0)

  useEffect(() => {
    if (!truncated) return
    const gen = ++generation.current
    setFetched(null)
    void (async () => {
      const all: LocationPointRead[] = []
      let partial = false
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
        if (!data) break
        all.push(...data.items)
        if (!data.has_more) break
        if (all.length >= MAX_POINTS) {
          partial = true
          break
        }
      }
      if (generation.current === gen) setFetched({ points: all, partial })
    })()
    return () => {
      generation.current++
    }
  }, [sessionId, truncated])

  if (!truncated) {
    return { points: events ? locationPoints(events) : [], partial: false, loading: !events }
  }
  return {
    points: fetched?.points ?? [],
    partial: fetched?.partial ?? false,
    loading: fetched === null,
  }
}
