import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react"
import { useSearchParams } from "react-router"
import { api, type SessionRead, type TimelineEvent, type TranscriptEvent } from "../api/client"
import { stopClip } from "../clipPlayer"
import { clock, fmtDate, shortId } from "../format"
import { usePlayhead } from "../hooks/usePlayhead"
import { fmtWallTime } from "../visual"
import AudioPlayer from "./AudioPlayer"
import Menu from "./Menu"
import PageHead from "./PageHead"
import Timeline, { hasContent } from "./Timeline"
import TimelineFilter, { ALL_KINDS, type EventKind, loadHidden } from "./TimelineFilter"
import StripView from "./TimelineStrip"

const TimelineStrip = memo(StripView)
const DAY_MS = 86_400_000
const SESSION_PAGE = 200
// Each timeline request assembles the whole recording server-side, so few big pages beat many small ones.
const EVENT_PAGE = 5000
// Session markers are replaced by the end-of-recording cue here.
const KINDS = ALL_KINDS.filter((kind) => kind.key !== "marker")

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

function title(session: SessionRead): string {
  return session.label ?? session.device ?? shortId(session.id)
}

/** The search string that scopes the timeline to one recording. */
export function recordingRange(session: SessionRead, seekMs?: number): string {
  const q = new URLSearchParams({
    from: session.started_at,
    to: session.ended_at ?? new Date().toISOString(),
    s: session.id,
  })
  if (seekMs !== undefined) q.set("t", String(Math.round(seekMs)))
  return `?${q}`
}

async function sessionsBetween(from: number, to: number): Promise<SessionRead[]> {
  const all: SessionRead[] = []
  for (;;) {
    const { data } = await api.GET("/v1/sessions", {
      params: {
        query: {
          since: new Date(from).toISOString(),
          until: new Date(to).toISOString(),
          limit: SESSION_PAGE,
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

type Span = { session: SessionRead; start: number; end: number }

/** Each recording's events inside [from, to), session-relative. */
async function eventsOf({ session, start, end }: Span, from: number, to: number) {
  const fromMs = from > start ? from - start : undefined
  const toMs = to < end ? to - start : undefined
  const all: TimelineEvent[] = []
  for (;;) {
    const { data } = await api.GET("/v1/sessions/{session_id}/timeline", {
      params: {
        path: { session_id: session.id },
        query: {
          limit: EVENT_PAGE,
          offset: all.length,
          ...(fromMs !== undefined ? { from_ms: fromMs } : {}),
          ...(toMs !== undefined ? { to_ms: toMs } : {}),
        },
      },
    })
    if (!data) break
    all.push(...data.items)
    if (!data.has_more) break
  }
  return all
}

// Loads recordings oldest first, so the list can start while later ones load.
function useRangeEvents(spans: Span[] | null, from: number, to: number, refreshKey: number) {
  const [byId, setById] = useState<Record<string, TimelineEvent[]>>({})
  const generation = useRef(0)

  useEffect(() => {
    if (!spans) return
    const gen = ++generation.current
    void (async () => {
      for (const span of spans) {
        const events = await eventsOf(span, from, to)
        if (generation.current !== gen) return
        setById((prev) => ({ ...prev, [span.session.id]: events }))
      }
    })()
  }, [spans, from, to, refreshKey])

  useEffect(() => setById({}), [spans])

  const reload = useCallback(
    (id: string) => {
      const span = spans?.find((s) => s.session.id === id)
      if (!span) return
      void eventsOf(span, from, to).then((events) =>
        setById((prev) => (id in prev ? { ...prev, [id]: events } : prev)),
      )
    },
    [spans, from, to],
  )
  return { byId, reload }
}

type Seek = (sessionId: string, ms: number, play?: boolean) => void

/** One recording's rows followed by its end cue. */
const Recording = memo(function Recording({
  span,
  events,
  index,
  to,
  focusMs,
  playheadMs,
  follow,
  hidden,
  onSeek,
  onDone,
  onSpeakersChanged,
  onEdited,
}: {
  span: Span
  events: TimelineEvent[]
  index: number
  to: number
  focusMs?: number
  playheadMs?: number
  follow: boolean
  hidden: Set<EventKind>
  onSeek: Seek
  onDone: (index: number) => void
  onSpeakersChanged: () => void
  onEdited: (sessionId: string) => void
}) {
  const { session } = span
  const seek = useCallback((ms: number) => onSeek(session.id, ms), [onSeek, session.id])
  const done = useCallback(() => onDone(index), [onDone, index])
  const edited = useCallback(() => onEdited(session.id), [onEdited, session.id])
  const content = useMemo(() => hasContent(events), [events])

  return (
    <section className="recording" aria-label={`Recording from ${fmtDate(session.started_at)}`}>
      <Timeline
        sessionId={session.id}
        startedAt={session.started_at}
        events={events}
        onSeek={seek}
        focusMs={focusMs}
        playheadMs={playheadMs}
        follow={follow}
        onSpeakersChanged={onSpeakersChanged}
        onEdited={edited}
        hidden={hidden}
        onDone={done}
      />
      {content && session.ended_at && span.end <= to && (
        <div className="recording-end">End of recording</div>
      )}
    </section>
  )
})

export default function TimelinePage() {
  const [params, setParams] = useSearchParams()
  const [now, setNow] = useState(() => Date.now())
  const to = parseParam(params.get("to")) ?? now
  const from = parseParam(params.get("from")) ?? to - DAY_MS
  const focusId = params.get("s")
  const focusT = Number(params.get("t"))
  const focusMs = params.has("t") && Number.isFinite(focusT) ? focusT : undefined

  const [spans, setSpans] = useState<Span[] | null>(null)
  // Recordings mount one after another as each finishes rendering, so the list reads in order.
  const [shown, setShown] = useState(1)
  const [hidden, setHidden] = useState<Set<EventKind>>(loadHidden)
  const [refreshKey, setRefreshKey] = useState(0)
  const [audioEl, setAudioEl] = useState<HTMLAudioElement | null>(null)
  const [playingId, setPlayingId] = useState<string | null>(null)
  const [pending, setPending] = useState<{ ms: number; play: boolean } | undefined>()
  const [follow, setFollow] = useState(true)
  const { byId, reload } = useRangeEvents(spans, from, to, refreshKey)

  useEffect(() => {
    stopClip()
  }, [])

  useEffect(() => {
    let stale = false
    setSpans(null)
    void sessionsBetween(from, to).then((list) => {
      if (stale) return
      const fetched = Date.now()
      const next = list.map((session) => ({
        session,
        start: Date.parse(session.started_at),
        end: session.ended_at ? Date.parse(session.ended_at) : fetched,
      }))
      const focus = next.findIndex((s) => s.session.id === focusId)
      setSpans(next)
      setShown(Math.max(1, focus + 1))
      setPlayingId(next[Math.max(0, focus)]?.session.id ?? null)
      if (focus >= 0 && focusMs !== undefined) setPending({ ms: focusMs, play: false })
    })
    return () => {
      stale = true
    }
    // The focus applies on arrival only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [from, to])

  const setRange = (key: "from" | "to", value: string) => {
    const ms = Date.parse(value)
    if (!Number.isFinite(ms)) return
    const next = new URLSearchParams({
      from: new Date(from).toISOString(),
      to: new Date(to).toISOString(),
    })
    next.set(key, new Date(ms).toISOString())
    setParams(next, { replace: true })
  }

  const onDone = useCallback((index: number) => setShown((n) => Math.max(n, index + 2)), [])
  const onSpeakersChanged = useCallback(() => setRefreshKey((k) => k + 1), [])

  const onSeek = useCallback<Seek>(
    (sessionId, ms, play = true) => {
      if (sessionId === playingId && audioEl && audioEl.readyState > 0) {
        audioEl.currentTime = ms / 1000
        if (play) void audioEl.play().catch(() => {})
        return
      }
      setPlayingId(sessionId)
      setPending({ ms, play })
    },
    [playingId, audioEl],
  )

  useEffect(() => {
    if (!audioEl || pending === undefined) return
    const apply = () => {
      audioEl.currentTime = pending.ms / 1000
      setPending(undefined)
      if (pending.play) void audioEl.play().catch(() => {})
    }
    if (audioEl.readyState > 0) {
      apply()
      return
    }
    audioEl.addEventListener("loadedmetadata", apply)
    return () => audioEl.removeEventListener("loadedmetadata", apply)
  }, [audioEl, pending, playingId])

  // Playback runs on into the next recording.
  useEffect(() => {
    if (!audioEl || !spans) return
    const onEnded = () => {
      const index = spans.findIndex((s) => s.session.id === playingId)
      const next = spans[index + 1]
      if (!next || next.start >= to) return
      setPlayingId(next.session.id)
      setPending({ ms: Math.max(0, from - next.start), play: true })
    }
    audioEl.addEventListener("ended", onEnded)
    return () => audioEl.removeEventListener("ended", onEnded)
  }, [audioEl, spans, playingId, from, to])

  // The strip lays every recording on one axis starting at the first one.
  const axis = useMemo(() => {
    if (!spans?.length) return null
    const start = Math.max(from, spans[0]!.start)
    const end = Math.min(to, Math.max(...spans.map((s) => s.end)))
    return { start, span: Math.max(1, end - start) }
  }, [spans, from, to])

  const strip = useMemo(() => {
    if (!axis || !spans) return null
    const owner = new WeakMap<TimelineEvent, string>()
    const events: TimelineEvent[] = []
    for (const { session, start } of spans) {
      const shift = start - axis.start
      for (const event of byId[session.id] ?? []) {
        const moved = {
          ...event,
          at_ms: event.at_ms + shift,
          ...("start_ms" in event ? { start_ms: event.start_ms + shift } : {}),
          ...("end_ms" in event ? { end_ms: event.end_ms + shift } : {}),
        } as TimelineEvent
        owner.set(moved, session.id)
        events.push(moved)
      }
    }
    const bands = spans.map((s) => ({
      startMs: Math.max(s.start, axis.start) - axis.start,
      endMs: Math.min(s.end, axis.start + axis.span) - axis.start,
    }))
    return { events, bands, scopeOf: (e: TranscriptEvent) => owner.get(e) ?? "" }
  }, [axis, spans, byId])

  const stripSeek = useCallback(
    (ms: number, play: boolean) => {
      if (!axis || !spans) return
      const at = axis.start + ms
      const hit =
        spans.find((s) => at >= s.start && at < s.end) ?? spans.find((s) => s.start > at)
      if (!hit) return
      onSeek(hit.session.id, Math.max(0, at - hit.start), play)
    },
    [axis, spans, onSeek],
  )
  const fmtAxis = useCallback(
    (ms: number, coarse?: boolean) => {
      const iso = new Date((axis?.start ?? 0) + ms).toISOString()
      return coarse ? clock(iso) : fmtWallTime(iso)
    },
    [axis],
  )

  const head = usePlayhead(audioEl, "coarse")
  const playing = spans?.find((s) => s.session.id === playingId)
  const loaded = spans?.filter((s) => byId[s.session.id]) ?? []
  const allRendered = spans !== null && loaded.length === spans.length && shown > spans.length

  return (
    <div className="timeline-page">
      <PageHead title="Timeline">
        {spans && spans.length > 0 && (
          <Menu
            label="Show one recording"
            trigger="Recordings ▾"
            items={spans.map(({ session }) => ({
              label: `${fmtDate(session.started_at)} · ${title(session)}`,
              to: `/timeline${recordingRange(session)}`,
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
          <button
            className="btn ghost slim"
            onClick={() => {
              setNow(Date.now())
              setParams({}, { replace: true })
            }}
          >
            Last 24 hours
          </button>
        )}
      </div>

      {strip && strip.events.length > 0 && axis && (
        <TimelineStrip
          events={strip.events}
          audioEl={audioEl}
          follow={follow}
          onFollowChange={setFollow}
          onSeek={stripSeek}
          onSpeakersChanged={onSpeakersChanged}
          spanMs={axis.span}
          offsetMs={playing ? playing.start - axis.start : 0}
          fmtTime={fmtAxis}
          scopeOf={strip.scopeOf}
          bands={strip.bands}
        />
      )}

      {!spans || (spans.length > 0 && loaded.length === 0) ? (
        <div className="list">
          {[36, 52, 36, 68, 36].map((height, i) => (
            <div key={i} className="skeleton" style={{ height }} />
          ))}
        </div>
      ) : spans.length === 0 ? (
        <div className="empty">Nothing was recorded in this range.</div>
      ) : (
        <div className="timeline-stitched">
          {spans.slice(0, shown).map((span, index) => {
            const events = byId[span.session.id]
            if (!events) return null
            const isPlaying = span.session.id === playingId
            return (
              <Recording
                key={span.session.id}
                span={span}
                events={events}
                index={index}
                to={to}
                focusMs={span.session.id === focusId ? focusMs : undefined}
                playheadMs={isPlaying ? head.ms : undefined}
                follow={isPlaying && follow && head.playing}
                hidden={hidden}
                onSeek={onSeek}
                onDone={onDone}
                onSpeakersChanged={onSpeakersChanged}
                onEdited={reload}
              />
            )
          })}
          {allRendered && !loaded.some((s) => hasContent(byId[s.session.id]!)) && (
            <div className="empty">Nothing on the timeline yet. Processing may still be running.</div>
          )}
        </div>
      )}

      {playing && (
        <div className="player-dock">
          <span className="player-dock-title">{title(playing.session)}</span>
          <AudioPlayer sessionId={playing.session.id} audioRef={setAudioEl} />
        </div>
      )}
    </div>
  )
}
