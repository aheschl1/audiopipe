import { memo, useCallback, useEffect, useMemo, useState } from "react"
import { useSearchParams } from "react-router"
import { api, type SessionRead } from "../api/client"
import { stopClip } from "../clipPlayer"
import { fmtDate, shortId } from "../format"
import { usePlayhead } from "../hooks/usePlayhead"
import AudioPlayer from "./AudioPlayer"
import Menu from "./Menu"
import PageHead from "./PageHead"
import Timeline from "./Timeline"
import TimelineFilter, { ALL_KINDS, type EventKind, loadHidden } from "./TimelineFilter"

const DAY_MS = 86_400_000
const PAGE = 200
// Session markers are replaced by the end-of-recording cue here.
const KINDS = ALL_KINDS.filter((kind) => kind.key !== "marker")

const List = memo(Timeline)

/** A Date as a `datetime-local` value, in local time. */
function toInput(ms: number): string {
  const d = new Date(ms - new Date(ms).getTimezoneOffset() * 60_000)
  return d.toISOString().slice(0, 16)
}

function parseParam(value: string | null): number | null {
  if (!value) return null
  const ms = Date.parse(value)
  return Number.isFinite(ms) ? ms : null
}

async function sessionsBetween(from: number, to: number): Promise<SessionRead[]> {
  const all: SessionRead[] = []
  for (;;) {
    const { data } = await api.GET("/v1/sessions", {
      params: {
        query: {
          since: new Date(from).toISOString(),
          until: new Date(to).toISOString(),
          limit: PAGE,
          offset: all.length,
        },
      },
    })
    if (!data) break
    all.push(...data.items)
    if (!data.has_more) break
  }
  return all.reverse()
}

function title(session: SessionRead): string {
  return session.label ?? session.device ?? shortId(session.id)
}

type Seek = (sessionId: string, ms: number) => void

/** One recording's rows, clipped to the range, followed by its end cue. */
const Recording = memo(function Recording({
  session,
  index,
  from,
  to,
  playheadMs,
  follow,
  hidden,
  refreshKey,
  onSeek,
  onDone,
  onSpeakersChanged,
}: {
  session: SessionRead
  index: number
  from: number
  to: number
  playheadMs?: number
  follow: boolean
  hidden: Set<EventKind>
  refreshKey: number
  onSeek: Seek
  onDone: (index: number, hasContent: boolean) => void
  onSpeakersChanged: () => void
}) {
  const [content, setContent] = useState<boolean | null>(null)
  const started = Date.parse(session.started_at)
  const ended = session.ended_at ? Date.parse(session.ended_at) : null
  const stitched = useMemo(
    () => ({
      startedAt: session.started_at,
      fromMs: from > started ? from - started : null,
      toMs: ended === null || to < ended ? to - started : null,
      onDone: (has: boolean) => {
        setContent(has)
        onDone(index, has)
      },
    }),
    [session.started_at, from, to, started, ended, index, onDone],
  )
  const seek = useCallback((ms: number) => onSeek(session.id, ms), [onSeek, session.id])

  return (
    <section className="recording" aria-label={`Recording from ${fmtDate(session.started_at)}`}>
      <List
        sessionId={session.id}
        onSeek={seek}
        playheadMs={playheadMs}
        follow={follow}
        refreshKey={refreshKey}
        onSpeakersChanged={onSpeakersChanged}
        hidden={hidden}
        stitched={stitched}
      />
      {content && ended !== null && ended <= to && <div className="recording-end">End of recording</div>}
    </section>
  )
})

export default function TimelinePage() {
  const [params, setParams] = useSearchParams()
  const [now, setNow] = useState(() => Date.now())
  const to = parseParam(params.get("to")) ?? now
  const from = parseParam(params.get("from")) ?? to - DAY_MS

  const [sessions, setSessions] = useState<SessionRead[] | null>(null)
  // Recordings mount one after another as each finishes loading, so the list reads in order.
  const [shown, setShown] = useState(1)
  const [anyContent, setAnyContent] = useState(false)
  const [hidden, setHidden] = useState<Set<EventKind>>(loadHidden)
  const [refreshKey, setRefreshKey] = useState(0)
  const [audioEl, setAudioEl] = useState<HTMLAudioElement | null>(null)
  const [playingId, setPlayingId] = useState<string | null>(null)
  const [pendingSeek, setPendingSeek] = useState<number | undefined>()

  useEffect(() => {
    stopClip()
  }, [])

  useEffect(() => {
    let stale = false
    setSessions(null)
    setShown(1)
    setAnyContent(false)
    void sessionsBetween(from, to).then((list) => {
      if (stale) return
      setSessions(list)
      setPlayingId((id) => (id && list.some((s) => s.id === id) ? id : (list[0]?.id ?? null)))
    })
    return () => {
      stale = true
    }
  }, [from, to])

  const setRange = (key: "from" | "to", value: string) => {
    const ms = Date.parse(value)
    if (!Number.isFinite(ms)) return
    const next = new URLSearchParams(params)
    next.set(key, new Date(ms).toISOString())
    if (!next.has(key === "from" ? "to" : "from"))
      next.set(key === "from" ? "to" : "from", new Date(key === "from" ? to : from).toISOString())
    setParams(next, { replace: true })
  }

  const onDone = useCallback((index: number, has: boolean) => {
    setShown((n) => Math.max(n, index + 2))
    if (has) setAnyContent(true)
  }, [])
  const onSpeakersChanged = useCallback(() => setRefreshKey((k) => k + 1), [])

  const startOf = useCallback(
    (session: SessionRead) => Math.max(0, from - Date.parse(session.started_at)),
    [from],
  )

  const onSeek = useCallback<Seek>(
    (sessionId, ms) => {
      if (sessionId === playingId && audioEl && audioEl.readyState > 0) {
        audioEl.currentTime = ms / 1000
        void audioEl.play().catch(() => {})
        return
      }
      setPlayingId(sessionId)
      setPendingSeek(ms)
    },
    [playingId, audioEl],
  )

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
  }, [audioEl, pendingSeek, playingId])

  // Playback runs on into the next recording.
  useEffect(() => {
    if (!audioEl || !sessions) return
    const onEnded = () => {
      const index = sessions.findIndex((s) => s.id === playingId)
      const next = sessions[index + 1]
      if (!next || Date.parse(next.started_at) >= to) return
      setPlayingId(next.id)
      setPendingSeek(startOf(next))
    }
    audioEl.addEventListener("ended", onEnded)
    return () => audioEl.removeEventListener("ended", onEnded)
  }, [audioEl, sessions, playingId, to, startOf])

  const head = usePlayhead(audioEl, "coarse")
  const playing = sessions?.find((s) => s.id === playingId)
  const visible = sessions?.slice(0, shown) ?? []
  const allLoaded = sessions !== null && shown > sessions.length

  return (
    <div className="timeline-page">
      <PageHead title="Timeline">
        {sessions && sessions.length > 0 && (
          <Menu
            label="Open a recording"
            trigger="Recordings ▾"
            items={sessions.map((session) => ({
              label: `${fmtDate(session.started_at)} · ${title(session)}`,
              to: `/sessions/${session.id}`,
            }))}
          />
        )}
        <TimelineFilter hidden={hidden} onChange={setHidden} kinds={KINDS} />
      </PageHead>
      <div className="range-bar">
        <label className="range-field">
          <span>From</span>
          <input
            className="input slim"
            type="datetime-local"
            value={toInput(from)}
            max={toInput(to)}
            onChange={(event) => setRange("from", event.target.value)}
          />
        </label>
        <label className="range-field">
          <span>To</span>
          <input
            className="input slim"
            type="datetime-local"
            value={toInput(to)}
            min={toInput(from)}
            onChange={(event) => setRange("to", event.target.value)}
          />
        </label>
        {(params.has("from") || params.has("to")) && (
          <button className="btn ghost slim" onClick={() => {
              setNow(Date.now())
              setParams({}, { replace: true })
            }}>
            Last 24 hours
          </button>
        )}
      </div>

      {!sessions ? (
        <div className="list">
          {[36, 52, 36, 68, 36].map((height, i) => (
            <div key={i} className="skeleton" style={{ height }} />
          ))}
        </div>
      ) : sessions.length === 0 ? (
        <div className="empty">Nothing was recorded in this range.</div>
      ) : (
        <div className="timeline-stitched">
          {visible.map((session, index) => (
            <Recording
              key={session.id}
              session={session}
              index={index}
              from={from}
              to={to}
              playheadMs={session.id === playingId ? head.ms : undefined}
              follow={session.id === playingId && head.playing}
              hidden={hidden}
              refreshKey={refreshKey}
              onSeek={onSeek}
              onDone={onDone}
              onSpeakersChanged={onSpeakersChanged}
            />
          ))}
          {allLoaded && !anyContent && (
            <div className="empty">Nothing on the timeline yet. Processing may still be running.</div>
          )}
        </div>
      )}

      {playing && (
        <div className="player-dock">
          <span className="player-dock-title">{title(playing)}</span>
          <AudioPlayer sessionId={playing.id} audioRef={setAudioEl} />
        </div>
      )}
    </div>
  )
}
