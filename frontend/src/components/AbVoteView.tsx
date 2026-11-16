import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { api, type AbSessionRead, type AbUtteranceRead } from "../api/client"
import { toggleClip, type Clip } from "../clipPlayer"
import ClipButton, { ClipProgress } from "./ClipButton"
import { fmtClock } from "../format"

const LETTERS = ["A", "B", "C", "D"] as const
const GENERATING_POLL_MS = 3000

function clipFor(sessionId: string, u: AbUtteranceRead): Clip {
  return { key: u.utterance_artifact_id, sessionId, startMs: u.start_ms, endMs: u.end_ms }
}

export default function AbVoteView({
  sessionId,
  onBack,
}: {
  sessionId: string
  onBack: () => void
}) {
  const [ab, setAb] = useState<AbSessionRead | null>(null)
  const [focus, setFocus] = useState(0)
  const [busy, setBusy] = useState(false)
  const [showVoted, setShowVoted] = useState(false)
  const cardRefs = useRef<Map<string, HTMLDivElement>>(new Map())

  const refresh = useCallback(async () => {
    const { data } = await api.GET("/v1/sessions/{session_id}/ab", {
      params: { path: { session_id: sessionId } },
    })
    if (data) setAb(data)
  }, [sessionId])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const generating = ab !== null && (ab.status === "queued" || ab.status === "running")
  useEffect(() => {
    if (!generating) return
    const timer = setInterval(() => void refresh(), GENERATING_POLL_MS)
    return () => clearInterval(timer)
  }, [generating, refresh])

  // Mid-generation, only complete quads are votable; a finished (possibly
  // degraded) run makes anything with candidates votable.
  const votable = useMemo(
    () =>
      (ab?.utterances ?? []).filter((u) =>
        generating ? u.candidates.length >= 4 : u.candidates.length > 0,
      ),
    [ab, generating],
  )
  const unvoted = useMemo(() => votable.filter((u) => !u.vote), [votable])
  const voted = useMemo(() => votable.filter((u) => u.vote), [votable])
  // The keyboard walks what's on screen: the queue, plus the voted section
  // when it's expanded.
  const visible = useMemo(
    () => (showVoted ? [...unvoted, ...voted] : unvoted),
    [showVoted, unvoted, voted],
  )

  useEffect(() => {
    setFocus((prev) => Math.min(prev, Math.max(0, visible.length - 1)))
  }, [visible.length])

  const vote = useCallback(
    async (utterance: AbUtteranceRead, candidateId: string) => {
      if (busy) return
      setBusy(true)
      const { data } = await api.POST("/v1/sessions/{session_id}/ab/votes", {
        params: { path: { session_id: sessionId } },
        body: {
          utterance_artifact_id: utterance.utterance_artifact_id,
          candidate_artifact_id: candidateId,
        },
      })
      setBusy(false)
      if (!data) return
      // The card slides into the voted section; the next unvoted card takes
      // its place at the same focus index.
      setAb((prev) =>
        prev
          ? {
              ...prev,
              voted: prev.voted + (utterance.vote ? 0 : 1),
              utterances: prev.utterances.map((u) =>
                u.utterance_artifact_id === utterance.utterance_artifact_id
                  ? { ...u, vote: { candidate_id: candidateId, model: data.model, strategy: data.strategy } }
                  : u,
              ),
            }
          : prev,
      )
    },
    [busy, sessionId],
  )

  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement
      if (target.tagName === "INPUT" || target.tagName === "TEXTAREA") return
      const current = visible[focus]
      if (event.key === "j" || event.key === "ArrowDown") {
        event.preventDefault()
        setFocus((prev) => Math.min(prev + 1, visible.length - 1))
      } else if (event.key === "k" || event.key === "ArrowUp") {
        event.preventDefault()
        setFocus((prev) => Math.max(prev - 1, 0))
      } else if (event.key === " " && current) {
        event.preventDefault()
        void toggleClip(clipFor(sessionId, current))
      } else if (["1", "2", "3", "4"].includes(event.key) && current) {
        const candidate = current.candidates[Number(event.key) - 1]
        if (candidate) void vote(current, candidate.candidate_id)
      }
    }
    document.addEventListener("keydown", handler)
    return () => document.removeEventListener("keydown", handler)
  }, [visible, focus, sessionId, vote])

  useEffect(() => {
    const current = visible[focus]
    if (!current) return
    cardRefs.current.get(current.utterance_artifact_id)?.scrollIntoView({ block: "nearest" })
  }, [focus, visible])

  const card = (utterance: AbUtteranceRead) => {
    const index = visible.indexOf(utterance)
    const clip = clipFor(sessionId, utterance)
    const revealed = utterance.vote
    return (
      <div
        key={utterance.utterance_artifact_id}
        ref={(el) => {
          if (el) cardRefs.current.set(utterance.utterance_artifact_id, el)
          else cardRefs.current.delete(utterance.utterance_artifact_id)
        }}
        className={`ab-card ${index === focus ? "focused" : ""} ${revealed ? "voted" : ""}`}
        onClick={() => index >= 0 && setFocus(index)}
      >
        <div className="row ab-head">
          <ClipButton clip={clip} />
          <span className="event-time as-span">{fmtClock(utterance.start_ms)}</span>
          <span className="row-dim">{fmtClock(utterance.end_ms - utterance.start_ms)}</span>
          {utterance.speaker && <span className="chip">{utterance.speaker}</span>}
          {revealed && (
            <span className="badge">
              {revealed.model} · {revealed.strategy}
            </span>
          )}
          <ClipProgress clip={clip} />
        </div>
        <div className="ab-cands">
          {utterance.candidates.map((candidate, ci) => {
            const winner = revealed?.candidate_id === candidate.candidate_id
            return (
              <button
                key={candidate.candidate_id}
                className={`ab-cand ${winner ? "winner" : ""}`}
                disabled={busy}
                onClick={() => void vote(utterance, candidate.candidate_id)}
              >
                <span className="rank">{LETTERS[ci] ?? ci + 1}</span>
                {candidate.text ? (
                  <span className="ab-cand-text">{candidate.text}</span>
                ) : (
                  <span className="ab-cand-text row-dim">(no words landed in this span)</span>
                )}
                {winner && <span className="chip strong">✓</span>}
              </button>
            )
          })}
        </div>
      </div>
    )
  }

  return (
    <div className="ab-view">
      <div className="ab-toolbar">
        <button className="btn ghost slim" onClick={onBack}>
          ← back
        </button>
        <span className="row-title">Transcription A/B</span>
        {ab && (
          <span className="chip strong">
            voted {ab.voted}/{votable.length || ab.total}
          </span>
        )}
        <span className="row-dim ab-keys">j/k move · 1-4 vote · space play</span>
      </div>

      {ab === null ? (
        <div className="list">
          {[72, 72, 72].map((height, i) => (
            <div key={i} className="skeleton" style={{ height }} />
          ))}
        </div>
      ) : generating ? (
        <div className="banner ab-progress-banner">
          <span className="spinner" />
          <span>
            generating candidates… {ab.candidates}/{ab.expected}
          </span>
          <span className="ab-bar">
            <span
              className="ab-bar-fill"
              style={{ width: `${Math.min(100, (ab.candidates / Math.max(1, ab.expected)) * 100)}%` }}
            />
          </span>
          <span className="row-dim">Cards appear here as they finish.</span>
        </div>
      ) : ab.status === "dead" ? (
        <div className="banner error">Couldn't generate candidates. Regenerate them from the A/B overview.</div>
      ) : votable.length === 0 ? (
        <div className="empty">
          No candidates yet. Enroll this session from the A/B tab.
        </div>
      ) : null}

      {ab !== null && !generating && votable.length > 0 && unvoted.length === 0 && (
        <div className="banner">All voted. Nothing left in this session.</div>
      )}

      <div className="list">{unvoted.map((utterance) => card(utterance))}</div>

      {voted.length > 0 && (
        <>
          <button
            className="btn ghost full ab-voted-toggle"
            onClick={() => setShowVoted((prev) => !prev)}
          >
            {showVoted ? "▾" : "▸"} voted ({voted.length})
          </button>
          {showVoted && <div className="list">{voted.map((utterance) => card(utterance))}</div>}
        </>
      )}
    </div>
  )
}
