import { useCallback, useState } from "react"
import { api, type SessionRead } from "../api/client"
import { dayKey, dayLabel, fmtDuration, shortId, timeOfDay } from "../format"
import { usePoll } from "../hooks/usePoll"
import { useReadOnly } from "../readOnly"
import PageHead from "./PageHead"

const PAGE = 50
const POLL_MS = 15_000

function groupByDay(sessions: SessionRead[]): { key: string; label: string; items: SessionRead[] }[] {
  const groups: { key: string; label: string; items: SessionRead[] }[] = []
  for (const session of sessions) {
    const key = dayKey(session.started_at)
    const last = groups[groups.length - 1]
    if (last?.key === key) last.items.push(session)
    else groups.push({ key, label: dayLabel(session.started_at), items: [session] })
  }
  return groups
}

export default function SessionList({ onOpen }: { onOpen: (id: string) => void }) {
  const [sessions, setSessions] = useState<SessionRead[] | null>(null)
  const [hasMore, setHasMore] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)
  const [splitting, setSplitting] = useState(false)
  const readOnly = useReadOnly()

  // The poll refreshes the first page only; pages loaded via "more" stay as
  // they are (newest-first offsets shift as sessions arrive — good enough
  // for a dev browser, and a failed refresh keeps the last good list).
  const refresh = useCallback(async () => {
    const { data } = await api.GET("/v1/sessions", {
      params: { query: { limit: PAGE, offset: 0 } },
    })
    if (!data) return
    setSessions((prev) => {
      if (!prev || prev.length <= PAGE) {
        setHasMore(data.has_more)
        return data.items
      }
      const seen = new Set(data.items.map((s) => s.id))
      return [...data.items, ...prev.filter((s) => !seen.has(s.id))]
    })
  }, [])
  usePoll(refresh, POLL_MS)

  const loadMore = async () => {
    if (!sessions) return
    setLoadingMore(true)
    const { data } = await api.GET("/v1/sessions", {
      params: { query: { limit: PAGE, offset: sessions.length } },
    })
    setLoadingMore(false)
    if (!data) return
    setHasMore(data.has_more)
    setSessions((prev) => {
      const seen = new Set((prev ?? []).map((s) => s.id))
      return [...(prev ?? []), ...data.items.filter((s) => !seen.has(s.id))]
    })
  }

  const splitAll = async () => {
    setSplitting(true)
    await api.POST("/v1/sessions/split", {})
    setSplitting(false)
    void refresh()
  }

  const live = sessions?.filter((s) => s.is_open).length ?? 0

  return (
    <div className="sessions-page">
      <PageHead
        title="Recordings"
        meta={
          sessions &&
          `${sessions.length}${hasMore ? "+" : ""} recorded${live ? ` · ${live} live` : ""}`
        }
      >
        {live > 0 && !readOnly && (
          <button
            className="btn slim"
            title="End every live session and start processing. Connected recorders reopen."
            disabled={splitting}
            onClick={() => void splitAll()}
          >
            {splitting ? "Splitting…" : "Split all live"}
          </button>
        )}
      </PageHead>

      {!sessions ? (
        <div className="list">
          {[64, 64, 64, 64, 64].map((height, i) => (
            <div key={i} className="skeleton" style={{ height }} />
          ))}
        </div>
      ) : sessions.length === 0 ? (
        <div className="empty">No recordings yet. Audio, photos and video from your phone or glasses land here.</div>
      ) : (
        groupByDay(sessions).map((group) => (
          <section key={group.key} className="day-group">
            <h2 className="day-label">{group.label}</h2>
            <div className="list">
              {group.items.map((session) => (
                <button
                  key={session.id}
                  className="row session-row"
                  onClick={() => onOpen(session.id)}
                >
                  <span className="session-time">{timeOfDay(session.started_at)}</span>
                  <div className="row-main">
                    <span className="row-title">
                      {session.label ?? session.device ?? shortId(session.id)}
                    </span>
                    <span className="row-sub">
                      {session.is_open ? "Recording" : fmtDuration(session.started_at, session.ended_at)}
                      {session.label && session.device && ` · ${session.device}`}
                    </span>
                  </div>
                  {session.is_open && <span className="badge live">Live</span>}
                </button>
              ))}
            </div>
          </section>
        ))
      )}
      {hasMore && (
        <button className="btn ghost full" onClick={loadMore} disabled={loadingMore}>
          {loadingMore ? "Loading…" : "Load older recordings"}
        </button>
      )}
    </div>
  )
}
