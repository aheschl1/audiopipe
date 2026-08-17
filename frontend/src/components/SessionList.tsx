import { useCallback, useState } from "react"
import { api, type SessionRead } from "../api/client"
import { fmtDate, fmtDuration, shortId } from "../format"
import { usePoll } from "../hooks/usePoll"

const PAGE = 50
const POLL_MS = 15_000

export default function SessionList({ onOpen }: { onOpen: (id: string) => void }) {
  const [sessions, setSessions] = useState<SessionRead[] | null>(null)
  const [hasMore, setHasMore] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)
  const [splitting, setSplitting] = useState(false)

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

  if (!sessions)
    return (
      <div className="list">
        {[64, 64, 64, 64, 64].map((height, i) => (
          <div key={i} className="skeleton" style={{ height }} />
        ))}
      </div>
    )
  if (sessions.length === 0) return <div className="empty">No sessions yet.</div>

  const splitAll = async () => {
    setSplitting(true)
    await api.POST("/v1/sessions/split", {})
    setSplitting(false)
    void refresh()
  }

  return (
    <div className="list">
      {sessions.some((s) => s.is_open) && (
        <button
          className="btn ghost full"
          title="end every live session and start processing; connected recorders reopen"
          disabled={splitting}
          onClick={() => void splitAll()}
        >
          {splitting ? "splitting…" : "split all live"}
        </button>
      )}
      {sessions.map((session) => (
        <button key={session.id} className="row" onClick={() => onOpen(session.id)}>
          <div className="row-main">
            <span className="row-title">
              {session.label ?? session.device ?? shortId(session.id)}
            </span>
            <span className="row-sub">
              {fmtDate(session.started_at)} · {fmtDuration(session.started_at, session.ended_at)}
            </span>
          </div>
          {session.is_open && <span className="badge live">live</span>}
        </button>
      ))}
      {hasMore && (
        <button className="btn ghost full" onClick={loadMore} disabled={loadingMore}>
          {loadingMore ? "loading…" : "more"}
        </button>
      )}
    </div>
  )
}
