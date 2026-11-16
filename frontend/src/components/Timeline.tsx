import { cloneElement, useCallback, useEffect, useMemo, useRef, useState, type ReactElement } from "react"
import {
  api,
  type ImageEvent,
  type LocationPointEvent,
  type TimelineEvent,
  type TranscriptEvent,
  type VideoClipEvent,
} from "../api/client"
import { fmtClock } from "../format"
import { describePoint, fmtCoords } from "../location"
import { audible, describeSpan } from "../sounds"
import { clipUrl, fmtWallTime, imageThumbUrl } from "../visual"
import SpeakerChip from "./SpeakerChip"
import TimelineFilter, { type EventKind } from "./TimelineFilter"
import VideoGutter from "./VideoGutter"
import VideoLightbox from "./VideoLightbox"

const PAGE = 200
// Deep links load the timeline from a little before the target moment, so
// the interesting event is on the first page even in a long session.
const FOCUS_LEAD_MS = 15_000

function eventKey(event: TimelineEvent, index: number): string {
  const source =
    "artifact_id" in event
      ? event.artifact_id
      : "segment_id" in event
        ? event.segment_id
        : "session"
  return `${source}-${event.type}-${event.at_ms}-${index}`
}

// Renders the session's event stream. The union is open by contract —
// unknown event types from future tiers are silently skipped (default
// branch), never an error.
export default function Timeline({
  sessionId,
  onSeek,
  focusMs,
  activeId,
  follow,
  refreshKey,
  onSpeakersChanged,
  onOpenImage,
  hidden,
  onHiddenChange,
}: {
  sessionId: string
  onSeek: (ms: number) => void
  focusMs?: number
  // `artifact_id + start_ms` of the utterance under the playhead, if any.
  activeId?: string | null
  // When true (playing + follow mode), the active row keeps itself in view.
  follow?: boolean
  refreshKey?: number
  onSpeakersChanged?: () => void
  onOpenImage?: (segmentId: string) => void
  hidden: Set<EventKind>
  onHiddenChange: (hidden: Set<EventKind>) => void
}) {
  const [events, setEvents] = useState<TimelineEvent[] | null>(null)
  const [hasMore, setHasMore] = useState(false)
  const [busy, setBusy] = useState(false)
  const [fromMs, setFromMs] = useState<number | null>(
    focusMs !== undefined ? Math.max(0, focusMs - FOCUS_LEAD_MS) : null,
  )
  const [flashKey, setFlashKey] = useState<string | null>(null)
  const flashed = useRef(false)
  // Location runs render collapsed; a click on the head row expands one.
  const [expandedRuns, setExpandedRuns] = useState<Set<string>>(new Set())
  // Inline transcript correction; keyed like the rows so only one is open.
  const [editingKey, setEditingKey] = useState<string | null>(null)
  const [editDraft, setEditDraft] = useState("")
  const [editBusy, setEditBusy] = useState(false)
  const wrapRef = useRef<HTMLDivElement | null>(null)
  const [gutterW, setGutterW] = useState(0)
  const [playing, setPlaying] = useState<{ clip: VideoClipEvent; offsetMs: number } | null>(null)
  const clips = useMemo(
    () =>
      hidden.has("video-clip")
        ? []
        : (events ?? []).filter((e): e is VideoClipEvent => e.type === "video-clip"),
    [events, hidden],
  )
  const openClip = useCallback(
    (clip: VideoClipEvent, offsetMs: number) => setPlaying({ clip, offsetMs }),
    [],
  )
  useEffect(() => {
    if (clips.length === 0) setGutterW(0)
  }, [clips.length])

  const load = useCallback(
    async (offset: number, windowStart: number | null) => {
      setBusy(true)
      const { data } = await api.GET("/v1/sessions/{session_id}/timeline", {
        params: {
          path: { session_id: sessionId },
          query: {
            limit: PAGE,
            offset,
            ...(windowStart !== null ? { from_ms: windowStart } : {}),
          },
        },
      })
      setBusy(false)
      if (!data) return
      setHasMore(data.has_more)
      setEvents((prev) => (offset === 0 ? data.items : [...(prev ?? []), ...data.items]))
    },
    [sessionId],
  )

  useEffect(() => {
    void load(0, fromMs)
  }, [load, fromMs])

  // After a speaker is labeled/merged, re-pull everything currently on
  // screen (not just page one) so names update without losing scroll depth.
  const loadedCount = useRef(0)
  useEffect(() => {
    loadedCount.current = events?.length ?? 0
  }, [events])
  const seenRefresh = useRef(refreshKey)
  useEffect(() => {
    if (refreshKey === undefined || refreshKey === seenRefresh.current) return
    seenRefresh.current = refreshKey
    void (async () => {
      const target = Math.max(PAGE, loadedCount.current)
      const all: TimelineEvent[] = []
      let more = false
      while (all.length < target) {
        const { data } = await api.GET("/v1/sessions/{session_id}/timeline", {
          params: {
            path: { session_id: sessionId },
            query: {
              limit: PAGE,
              offset: all.length,
              ...(fromMs !== null ? { from_ms: fromMs } : {}),
            },
          },
        })
        if (!data) break
        all.push(...data.items)
        more = data.has_more
        if (!data.has_more) break
      }
      setEvents(all)
      setHasMore(more)
    })()
  }, [refreshKey, sessionId, fromMs])

  // Flash-and-scroll the first substantive event at/after the focus moment,
  // once per deep link.
  useEffect(() => {
    if (focusMs === undefined || flashed.current || !events) return
    const index = events.findIndex(
      (event) =>
        (event.type === "transcript" || event.type === "audio-tag") &&
        event.at_ms >= focusMs,
    )
    if (index >= 0) {
      flashed.current = true
      setFlashKey(eventKey(events[index]!, index))
    }
  }, [events, focusMs])

  const flashRef = useCallback((node: HTMLDivElement | null) => {
    node?.scrollIntoView({ block: "center", behavior: "smooth" })
  }, [])

  // Save a transcript correction: the row updates in place for immediate
  // feedback, then the shared refresh re-pulls so the strip (and any other
  // consumer of the session's events) serves the same text.
  const saveEdit = async (event: TranscriptEvent) => {
    const text = editDraft.trim()
    if (!text || text === event.text) {
      setEditingKey(null)
      return
    }
    setEditBusy(true)
    const { data } = await api.POST("/v1/sessions/{session_id}/transcripts/{artifact_id}", {
      params: { path: { session_id: sessionId, artifact_id: event.artifact_id } },
      body: { text },
    })
    setEditBusy(false)
    if (!data) return // keep the editor open so the correction isn't lost
    setEvents(
      (prev) =>
        prev?.map((e) => (e === event ? { ...e, text, chars: text.length } : e)) ?? prev,
    )
    setEditingKey(null)
    onSpeakersChanged?.()
  }

  // Follow mode: as playback crosses into a new utterance, nudge its row
  // into view. block:"nearest" keeps this gentle — no scroll when visible.
  useEffect(() => {
    if (!follow || !activeId) return
    document
      .querySelector(".event.playing")
      ?.scrollIntoView({ block: "nearest", behavior: "smooth" })
  }, [follow, activeId])

  if (!events)
    return (
      <div className="list">
        {[36, 52, 36, 68, 36].map((height, i) => (
          <div key={i} className="skeleton" style={{ height }} />
        ))}
      </div>
    )

  const rendered = events.map((event, index) => {
    const key = eventKey(event, index)
    const flash = key === flashKey
    switch (event.type) {
      case "transcript": {
        if (hidden.has("transcript")) return null
        const active = activeId != null && activeId === event.artifact_id + event.start_ms
        const interjection = event.interjection_of != null
        return (
          <div
            key={key}
            ref={flash ? flashRef : undefined}
            className={`event transcript ${interjection ? "interjection" : ""} ${flash ? "flash" : ""} ${active ? "playing" : ""}`}
          >
            <button
              className="event-time"
              title="Listen from here"
              onClick={() => onSeek(event.at_ms)}
            >
              {fmtClock(event.at_ms)}
            </button>
            <div className="event-body">
              {(event.speaker || event.speaker_id) && (
                <SpeakerChip
                  clusterId={event.speaker_id}
                  name={event.speaker_name}
                  localLabel={event.speaker}
                  memberIds={event.voiceprint_id ? [event.voiceprint_id] : undefined}
                  onChanged={() => onSpeakersChanged?.()}
                />
              )}
              {editingKey === key ? (
                <form
                  className="transcript-edit"
                  onSubmit={(e) => {
                    e.preventDefault()
                    void saveEdit(event)
                  }}
                >
                  <textarea
                    className="input slim"
                    value={editDraft}
                    autoFocus
                    rows={2}
                    onChange={(e) => setEditDraft(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Escape") setEditingKey(null)
                      if (e.key === "Enter" && !e.shiftKey) {
                        e.preventDefault()
                        void saveEdit(event)
                      }
                    }}
                  />
                  <button className="btn primary slim" disabled={editBusy}>
                    {editBusy ? "Saving…" : "Save"}
                  </button>
                  <button
                    type="button"
                    className="btn ghost slim"
                    disabled={editBusy}
                    onClick={() => setEditingKey(null)}
                  >
                    Cancel
                  </button>
                </form>
              ) : (
                <>
                  <span className="transcript-text">{event.text}</span>
                  <button
                    className="btn ghost slim event-edit"
                    title="Correct this transcript"
                    onClick={() => {
                      setEditDraft(event.text)
                      setEditingKey(key)
                    }}
                  >
                    ✎
                  </button>
                </>
              )}
            </div>
          </div>
        )
      }
      case "audio-tag": {
        const tags = event.labels.filter((tag) => audible(tag.score))
        if (hidden.has("audio-tag") || tags.length === 0) return null
        return (
          <div
            key={key}
            ref={flash ? flashRef : undefined}
            className={`event tags ${flash ? "flash" : ""}`}
          >
            <button
              className="event-time"
              title="Listen from here"
              onClick={() => onSeek(event.at_ms)}
            >
              {fmtClock(event.at_ms)}
            </button>
            <div className="event-body chips">
              {tags.slice(0, 5).map((tag) => (
                <span key={tag.label} className="chip">
                  {tag.label}
                </span>
              ))}
            </div>
          </div>
        )
      }
      case "conversation": {
        if (hidden.has("conversation")) return null
        const voices = event.speaker_count
        return (
          <div
            key={key}
            ref={flash ? flashRef : undefined}
            className={`event conversation ${flash ? "flash" : ""}`}
          >
            <button
              className="event-time"
              title="Listen from here"
              onClick={() => onSeek(event.at_ms)}
            >
              {fmtClock(event.at_ms)}
            </button>
            <div className="event-body conversation-head">
              <span className="conversation-label">Conversation</span>
              <span className="row-dim">
                {event.turns} turn{event.turns === 1 ? "" : "s"} · {voices} voice
                {voices === 1 ? "" : "s"}
                {event.alternations === 0 && voices <= 1 ? " · one-sided" : ""} ·{" "}
                {fmtClock(event.end_ms - event.start_ms)}
              </span>
            </div>
          </div>
        )
      }
      case "sound-span": {
        if (hidden.has("sound-span") || !audible(event.peak)) return null
        return (
          <div
            key={key}
            ref={flash ? flashRef : undefined}
            className={`event tags ${flash ? "flash" : ""}`}
          >
            <button
              className="event-time"
              title="Listen from here"
              onClick={() => onSeek(event.at_ms)}
            >
              {fmtClock(event.at_ms)}
            </button>
            <div className="event-body chips">
              <span className="chip strong" title={describeSpan(event)}>
                {event.label}
              </span>
              <span className="row-dim">
                {fmtClock(event.start_ms)}–{fmtClock(event.end_ms)} ·{" "}
                {fmtClock(event.end_ms - event.start_ms)}
              </span>
            </div>
          </div>
        )
      }
      case "location-point": {
        if (hidden.has("location-point")) return null
        // Consecutive points collapse into one expandable row (coalescing
        // lives in the render pass, so windowing/pagination are untouched);
        // only the run's first event renders.
        if (index > 0 && events[index - 1]?.type === "location-point") return null
        let end = index
        while (end + 1 < events.length && events[end + 1]?.type === "location-point") end++
        const run = events.slice(index, end + 1) as LocationPointEvent[]
        const first = run[0]!
        const last = run[run.length - 1]!
        const expanded = expandedRuns.has(key)
        return (
          <div key={key} ref={flash ? flashRef : undefined} className={`event tags ${flash ? "flash" : ""}`}>
            <button
              className="event-time"
              title="Listen from here"
              onClick={() => onSeek(Math.max(0, first.at_ms))}
            >
              {fmtClock(Math.max(0, first.at_ms))}
            </button>
            <div className="event-body">
              <button
                className="chip as-button"
                title={expanded ? "Collapse" : "Show every point"}
                onClick={() =>
                  setExpandedRuns((prev) => {
                    const next = new Set(prev)
                    if (next.has(key)) next.delete(key)
                    else next.add(key)
                    return next
                  })
                }
              >
                📍 {run.length === 1 ? fmtCoords(first) : `${run.length} points`}
              </button>
              {run.length > 1 && (
                <span className="row-dim">
                  {" "}{fmtCoords(first)} → {fmtCoords(last)} ·{" "}
                  {fmtClock(Math.max(0, first.at_ms))}–{fmtClock(Math.max(0, last.at_ms))}
                </span>
              )}
              {run.length === 1 && first.accuracy_m != null && (
                <span className="row-dim"> ±{Math.round(first.accuracy_m)} m</span>
              )}
              {expanded && run.length > 1 && (
                <div className="list">
                  {run.map((point, i) => (
                    <div key={`${point.segment_id}-${point.at_ms}-${i}`} className="row-dim">
                      <button
                        className="event-time"
                        title="Listen from here"
                        onClick={() => onSeek(Math.max(0, point.at_ms))}
                      >
                        {fmtClock(Math.max(0, point.at_ms))}
                      </button>{" "}
                      {describePoint(point)}
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        )
      }
      case "image": {
        if (hidden.has("image")) return null
        // Consecutive images render as one gallery row, like location runs.
        if (index > 0 && events[index - 1]?.type === "image") return null
        let end = index
        while (end + 1 < events.length && events[end + 1]?.type === "image") end++
        const run = events.slice(index, end + 1) as ImageEvent[]
        const at = Math.max(0, run[0]!.at_ms)
        return (
          <div key={key} ref={flash ? flashRef : undefined} className={`event visual ${flash ? "flash" : ""}`}>
            <button className="event-time" title="Listen from here" onClick={() => onSeek(at)}>
              {fmtClock(at)}
            </button>
            <div className="event-body gallery">
              {run.map((image) => (
                <button
                  key={image.segment_id}
                  className="thumb"
                  title={`${fmtWallTime(image.captured_at)} · open`}
                  onClick={() => onOpenImage?.(image.segment_id)}
                >
                  {imageThumbUrl(image) ? (
                    <img
                      src={imageThumbUrl(image)}
                      alt={`Image at ${fmtClock(Math.max(0, image.at_ms))}`}
                      loading="lazy"
                    />
                  ) : (
                    <span className="thumb-placeholder" />
                  )}
                </button>
              ))}
            </div>
          </div>
        )
      }
      case "video-clip":
        // Drawn in the gutter alongside the rows it spans, not as a row.
        return null
      case "session-start":
        if (hidden.has("marker")) return null
        return (
          <div key={key} className="event marker">
            Session started
          </div>
        )
      case "session-end":
        if (hidden.has("marker")) return null
        return (
          <div key={key} className="event marker">
            session ended · {fmtClock(event.at_ms)}
          </div>
        )
      default:
        // speech-start/speech-end and any future event types: not rendered.
        return null
    }
  })

  // Rows carry their position so the video gutter can line clips up with them.
  const tagged = rendered.map((node, index) =>
    node ? cloneElement(node as ReactElement<{ "data-at"?: number }>, { "data-at": events[index]!.at_ms }) : null,
  )

  const allFilteredOut =
    rendered.every((node) => node === null) && hidden.size > 0 && events.length > 0

  return (
    <>
      <div className="timeline-toolbar">
        <TimelineFilter hidden={hidden} onChange={onHiddenChange} />
      </div>
      <div
        className="timeline-wrap"
        ref={wrapRef}
        style={{ paddingLeft: gutterW ? gutterW + 4 : 0 }}
      >
        {clips.length > 0 && (
          <VideoGutter
            clips={clips}
            wrapRef={wrapRef}
            onOpen={openClip}
            onWidth={setGutterW}
          />
        )}
      <div className="timeline">
        {fromMs !== null && fromMs > 0 && (
          <button
            className="btn ghost full"
            onClick={() => {
              flashed.current = true // keep the view where the user is
              setFlashKey(null)
              setEvents(null)
              setFromMs(null)
            }}
          >
            ↑ show from the beginning (jumped to {fmtClock(fromMs + FOCUS_LEAD_MS)})
          </button>
        )}
        {rendered.every((node) => node === null) ? (
          <div className="empty">
            {allFilteredOut
              ? "Everything here is hidden by the filter."
              : "Nothing on the timeline yet. Processing may still be running."}
          </div>
        ) : (
          tagged
        )}
        {hasMore && (
          <button
            className="btn ghost full"
            onClick={() => load(events.length, fromMs)}
            disabled={busy}
          >
            {busy ? "Loading…" : "More"}
          </button>
        )}
      </div>
      </div>
      {playing && (
        <VideoLightbox
          clip={playing.clip}
          src={clipUrl(playing.clip)}
          offsetMs={playing.offsetMs}
          onClose={() => setPlaying(null)}
          onSeek={onSeek}
        />
      )}
    </>
  )
}
