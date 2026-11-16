import { useCallback, useEffect, useState } from "react"
import { api, type AbResultsRead, type SessionRead } from "../api/client"
import { fmtDate, fmtDuration, shortId } from "../format"
import { usePoll } from "../hooks/usePoll"
import PageHead from "./PageHead"

const POLL_MS = 10_000
const ACTIVE_POLL_MS = 2_500

type State = AbResultsRead["sessions"][number]

function StatusChip({ state }: { state: State }) {
  if (state.status === "queued")
    return (
      <span className="chip ab-generating">
        <span className="spinner" /> Queued…
      </span>
    )
  if (state.status === "running")
    return (
      <span className="chip ab-generating">
        <span className="spinner" />
        <span className="ab-mini">
          <span
            className="ab-mini-fill"
            style={{ width: `${Math.min(100, (state.candidates / Math.max(1, state.expected)) * 100)}%` }}
          />
        </span>
        {state.candidates}/{state.expected}
      </span>
    )
  if (state.status === "dead") return <span className="chip ab-dead">Failed</span>
  return <span className="chip">{state.candidates} candidates</span>
}

function isDone(state: State | undefined): boolean {
  return (
    state !== undefined &&
    state.status === "succeeded" &&
    state.votable > 0 &&
    state.votes >= state.votable
  )
}

export default function AbOverview({ onVote }: { onVote: (sessionId: string) => void }) {
  const [results, setResults] = useState<AbResultsRead | null>(null)
  const [sessions, setSessions] = useState<SessionRead[] | null>(null)
  const [queued, setQueued] = useState<Record<string, boolean>>({})
  const [showDone, setShowDone] = useState(false)

  const refresh = useCallback(async () => {
    const [tally, list] = await Promise.all([
      api.GET("/v1/ab/results"),
      api.GET("/v1/sessions", { params: { query: { limit: 200, offset: 0 } } }),
    ])
    if (tally.data) {
      setResults(tally.data)
      // The server state supersedes the optimistic "queued" flags.
      setQueued((prev) => {
        const next = { ...prev }
        for (const s of tally.data.sessions) delete next[s.session_id]
        return next
      })
    }
    if (list.data) setSessions(list.data.items)
  }, [])
  usePoll(refresh, POLL_MS)

  // Tighten the loop while anything is generating so progress moves live.
  const active =
    Object.keys(queued).length > 0 ||
    (results?.sessions.some((s) => s.status === "queued" || s.status === "running") ?? false)
  useEffect(() => {
    if (!active) return
    const timer = setInterval(() => void refresh(), ACTIVE_POLL_MS)
    return () => clearInterval(timer)
  }, [active, refresh])

  const enroll = async (sessionId: string) => {
    setQueued((prev) => ({ ...prev, [sessionId]: true }))
    const { data } = await api.POST("/v1/sessions/{session_id}/ab", {
      params: { path: { session_id: sessionId } },
    })
    if (!data) {
      setQueued((prev) => {
        const next = { ...prev }
        delete next[sessionId]
        return next
      })
      return
    }
    void refresh()
  }

  const stateFor = (sessionId: string) =>
    results?.sessions.find((s) => s.session_id === sessionId)

  return (
    <div className="ab-overview">
      <PageHead title="A/B transcripts" meta="Vote on competing transcriptions to pick the best model." />
      <div className="ab-tally">
        <div className="row-title">Wins by model and strategy</div>
        {results === null ? (
          <div className="skeleton" style={{ height: 60 }} />
        ) : results.total === 0 ? (
          <div className="empty">No votes yet — generate candidates for a session, then vote.</div>
        ) : (
          <table className="ab-tally-table">
            <thead>
              <tr>
                <th>Model</th>
                <th>Strategy</th>
                <th>Wins</th>
                <th>Share</th>
              </tr>
            </thead>
            <tbody>
              {results.tally.map((row) => (
                <tr key={`${row.model}:${row.strategy}`}>
                  <td>{row.model}</td>
                  <td>{row.strategy}</td>
                  <td>{row.wins}</td>
                  <td>
                    <span className="ab-share">
                      <span
                        className="ab-share-fill"
                        style={{ width: `${(row.wins / results.total) * 100}%` }}
                      />
                    </span>
                    {Math.round((row.wins / results.total) * 100)}%
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {results !== null && results.total > 0 && (
          <div className="row-dim">{results.total} vote(s) total</div>
        )}
      </div>

      {sessions === null ? (
        <div className="list">
          {[64, 64, 64].map((height, i) => (
            <div key={i} className="skeleton" style={{ height }} />
          ))}
        </div>
      ) : sessions.length === 0 ? (
        <div className="empty">No sessions yet.</div>
      ) : (
        (() => {
          const open = sessions.filter((s) => !isDone(stateFor(s.id)))
          const done = sessions.filter((s) => isDone(stateFor(s.id)))
          const row = (session: SessionRead) => {
            const state = stateFor(session.id)
            const busy =
              queued[session.id] || state?.status === "queued" || state?.status === "running"
            return (
              <div key={session.id} className="row ab-session-row">
                <div className="row-main">
                  <span className="row-title">{session.label ?? shortId(session.id)}</span>
                  <span className="row-sub">
                    {fmtDate(session.started_at)} · {fmtDuration(session.started_at, session.ended_at)}
                  </span>
                </div>
                {state ? <StatusChip state={state} /> : null}
                {queued[session.id] && !state && (
                  <span className="chip ab-generating">
                    <span className="spinner" /> Queued…
                  </span>
                )}
                {(state?.votes ?? 0) > 0 && (
                  <span className="chip strong">
                    {state!.votes}/{state!.votable} voted
                  </span>
                )}
                <button
                  className="btn ghost slim"
                  disabled={busy}
                  onClick={() => void enroll(session.id)}
                >
                  {busy ? "Generating…" : state ? "Regenerate" : "Generate"}
                </button>
                <button
                  className="btn primary slim"
                  disabled={!state || state.candidates === 0}
                  onClick={() => onVote(session.id)}
                >
                  Vote
                </button>
              </div>
            )
          }
          return (
            <>
              <div className="list">
                {open.length === 0 ? (
                  <div className="empty">Every enrolled session is fully voted.</div>
                ) : (
                  open.map(row)
                )}
              </div>
              {done.length > 0 && (
                <>
                  <button
                    className="btn ghost full ab-voted-toggle"
                    onClick={() => setShowDone((prev) => !prev)}
                  >
                    {showDone ? "▾" : "▸"} done voting ({done.length})
                  </button>
                  {showDone && <div className="list">{done.map(row)}</div>}
                </>
              )}
            </>
          )
        })()
      )}
    </div>
  )
}
