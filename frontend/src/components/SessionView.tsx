import { memo, useCallback, useEffect, useMemo, useState } from "react"
import { api, type SessionRead } from "../api/client"
import { stopClip } from "../clipPlayer"
import { fmtDate, fmtDuration, shortId } from "../format"
import { usePlayhead } from "../hooks/usePlayhead"
import { useSessionEvents } from "../hooks/useSessionEvents"
import AudioPlayer from "./AudioPlayer"
import SessionMapPanel from "./SessionMapPanel"
import TagSummary from "./TagSummary"
import Timeline from "./Timeline"
import TimelineStrip from "./TimelineStrip"

// Memoized so the coarse playhead ticking in this component only re-renders
// the list when the active utterance actually changes.
const TimelineList = memo(Timeline)

export default function SessionView({
  sessionId,
  seekMs,
  onBack,
  onAb,
}: {
  sessionId: string
  seekMs?: number
  onBack: () => void
  onOpenSession: (id: string, seekMs?: number) => void
  onAb?: (id: string) => void
}) {
  const [session, setSession] = useState<SessionRead | null>(null)
  // Rename draft; null while the title is just being displayed.
  const [draft, setDraft] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [splitting, setSplitting] = useState(false)
  // The audio element as state (not a ref): the strip and playhead hooks
  // need to re-subscribe when it mounts.
  const [audioEl, setAudioEl] = useState<HTMLAudioElement | null>(null)
  const [pendingSeek, setPendingSeek] = useState<number | undefined>(seekMs)
  const [follow, setFollow] = useState(true)
  const [showMap, setShowMap] = useState(false)
  // Bumped after a speaker is labeled/merged anywhere on the page, so names
  // propagate to the strip, the transcript list, and the lane headers.
  const [refreshKey, setRefreshKey] = useState(0)

  const { events, truncated } = useSessionEvents(sessionId, refreshKey)

  // The session has its own full player; a still-running inline clip from
  // the search/speakers page would double the audio.
  useEffect(() => {
    stopClip()
  }, [])

  useEffect(() => {
    void api
      .GET("/v1/sessions/{session_id}", {
        params: { path: { session_id: sessionId } },
      })
      .then(({ data }) => {
        if (data) setSession(data)
      })
  }, [sessionId])

  const seekTo = useCallback(
    (ms: number, play = true) => {
      if (!audioEl) return
      if (audioEl.readyState === 0) {
        setPendingSeek(ms)
        return
      }
      audioEl.currentTime = ms / 1000
      if (play) void audioEl.play().catch(() => {})
    },
    [audioEl],
  )

  // A seek requested before the element had metadata (deep link from search)
  // applies once the audio is ready.
  useEffect(() => {
    if (!audioEl || pendingSeek === undefined) return
    const apply = () => {
      audioEl.currentTime = pendingSeek / 1000
      setPendingSeek(undefined)
      void audioEl.play().catch(() => {})
    }
    if (audioEl.readyState > 0) {
      apply()
      return
    }
    audioEl.addEventListener("loadedmetadata", apply)
    return () => audioEl.removeEventListener("loadedmetadata", apply)
  }, [audioEl, pendingSeek])

  // Coarse (quantized) playhead — just enough to know which utterance is
  // under the needle for the transcript list highlight.
  const head = usePlayhead(audioEl, "coarse")
  const activeId = useMemo(() => {
    if (!events) return null
    for (const event of events) {
      if (
        event.type === "transcript" &&
        head.ms >= event.start_ms &&
        head.ms < event.end_ms
      ) {
        return event.artifact_id + event.start_ms
      }
    }
    return null
  }, [events, head.ms])

  const onSpeakersChanged = useCallback(() => setRefreshKey((k) => k + 1), [])

  // An empty draft clears the label, so the title falls back to the device
  // (or the short id) exactly as it did before the session was ever named.
  const saveLabel = async () => {
    if (draft === null) return
    const label = draft.trim() || null
    if (label === (session?.label ?? null)) {
      setDraft(null)
      return
    }
    setSaving(true)
    const { data } = await api.POST("/v1/sessions/{session_id}/label", {
      params: { path: { session_id: sessionId } },
      body: { label },
    })
    setSaving(false)
    if (!data) return // keep the draft open so the text isn't lost
    setSession(data)
    setDraft(null)
  }

  const splitSession = async () => {
    setSplitting(true)
    const { data } = await api.POST("/v1/sessions/{session_id}/split", {
      params: { path: { session_id: sessionId } },
    })
    setSplitting(false)
    if (data) setSession(data)
  }

  if (!session) return <div className="loading">Loading session…</div>

  return (
    <div className="session-view">
      <div className="session-header">
        <button className="btn ghost slim" onClick={onBack}>
          ← back
        </button>
        <div>
          {draft === null ? (
            <h2 className="session-title">
              {session.label ?? session.device ?? shortId(session.id)}
              {session.is_open && <span className="badge live">live</span>}
              <button
                className="btn ghost slim"
                title="rename this session"
                onClick={() => setDraft(session.label ?? "")}
              >
                rename
              </button>
              {session.is_open && (
                <button
                  className="btn ghost slim"
                  title="end this session and start processing; a connected recorder reopens"
                  disabled={splitting}
                  onClick={() => void splitSession()}
                >
                  {splitting ? "splitting…" : "split"}
                </button>
              )}
              {onAb && (
                <button
                  className="btn ghost slim"
                  title="vote on transcript candidates"
                  onClick={() => onAb(session.id)}
                >
                  A/B
                </button>
              )}
            </h2>
          ) : (
            <form
              className="session-rename"
              onSubmit={(event) => {
                event.preventDefault()
                void saveLabel()
              }}
            >
              <input
                className="input slim"
                placeholder="name this session…"
                value={draft}
                autoFocus
                maxLength={200}
                onChange={(event) => setDraft(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Escape") setDraft(null)
                }}
              />
              <button className="btn primary slim" disabled={saving}>
                {saving ? "saving…" : "save"}
              </button>
              <button
                type="button"
                className="btn ghost slim"
                disabled={saving}
                onClick={() => setDraft(null)}
              >
                cancel
              </button>
            </form>
          )}
          <div className="session-sub">
            {fmtDate(session.started_at)} · {fmtDuration(session.started_at, session.ended_at)}
            {session.device && ` · ${session.device}`}
          </div>
        </div>
      </div>
      <AudioPlayer sessionId={sessionId} audioRef={setAudioEl} />
      {events && events.length > 0 && (
        <TimelineStrip
          events={events}
          truncated={truncated}
          audioEl={audioEl}
          follow={follow}
          onFollowChange={setFollow}
          onSeek={seekTo}
          onSpeakersChanged={onSpeakersChanged}
          mapOpen={showMap}
          onMapToggle={() => setShowMap((v) => !v)}
        />
      )}
      {showMap && (
        <SessionMapPanel
          sessionId={sessionId}
          events={events}
          truncated={truncated}
          audioEl={audioEl}
          onSeek={seekTo}
        />
      )}
      <TagSummary sessionId={sessionId} />
      <TimelineList
        sessionId={sessionId}
        onSeek={seekTo}
        focusMs={seekMs}
        activeId={activeId}
        follow={follow && head.playing}
        refreshKey={refreshKey}
        onSpeakersChanged={onSpeakersChanged}
      />
    </div>
  )
}
