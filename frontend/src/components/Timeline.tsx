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
import { clipUrl, fmtWallTime, imageEvents, imageThumbUrl } from "../visual"
import ImageLightbox from "./ImageLightbox"
import SpeakerChip from "./SpeakerChip"
import { type EventKind } from "./TimelineFilter"
import VideoGutter from "./VideoGutter"
import VideoLightbox from "./VideoLightbox"

// Rows render in chunks as the end of the list nears; long recordings hold thousands.
const CHUNK = 300
// Frames that carry no content of their own.
const QUIET = new Set(["session-start", "session-end", "speech-start", "speech-end"])

function eventKey(event: TimelineEvent, index: number): string {
  const source =
    "artifact_id" in event
      ? event.artifact_id
      : "segment_id" in event
        ? event.segment_id
        : "session"
  return `${source}-${event.type}-${event.at_ms}-${index}`
}

/** True when a recording's events hold anything beyond its frames. */
export function hasContent(events: TimelineEvent[]): boolean {
  return events.some((event) => !QUIET.has(event.type))
}

// Renders one recording's event stream with wall-clock times. The union is
// open by contract — unknown event types from future tiers are silently
// skipped (default branch), never an error.
export default function Timeline({
  sessionId,
  startedAt,
  events: all,
  onSeek,
  focusMs,
  playheadMs,
  follow,
  onSpeakersChanged,
  onEdited,
  hidden,
  onDone,
}: {
  sessionId: string
  startedAt: string
  events: TimelineEvent[]
  onSeek: (ms: number) => void
  // Scroll to and flash the first substantive event at/after this moment.
  focusMs?: number
  // Session-relative playhead; highlights the utterance under it.
  playheadMs?: number
  // When true (playing + follow mode), the active row keeps itself in view.
  follow?: boolean
  onSpeakersChanged?: () => void
  onEdited?: () => void
  hidden: Set<EventKind>
  // Fires once every row is rendered, so the next recording can follow.
  onDone?: () => void
}) {
  const focusIndex = useMemo(
    () =>
      focusMs === undefined
        ? -1
        : all.findIndex(
            (event) =>
              (event.type === "transcript" || event.type === "audio-tag") &&
              !hidden.has(event.type) &&
              event.at_ms >= focusMs,
          ),
    [all, focusMs, hidden],
  )
  const [limit, setLimit] = useState(() => Math.max(CHUNK, focusIndex + CHUNK))
  const events = useMemo(() => all.slice(0, limit), [all, limit])
  const hasMore = all.length > limit
  // Corrections show at once; the refetch they trigger catches the rest up.
  const [edits, setEdits] = useState<Map<string, string>>(new Map())
  const [flashKey, setFlashKey] = useState<string | null>(null)
  const flashed = useRef(false)
  // Location runs render collapsed; a click on the head row expands one.
  const [expandedRuns, setExpandedRuns] = useState<Set<string>>(new Set())
  const [lightboxIndex, setLightboxIndex] = useState<number | null>(null)
  const images = useMemo(() => imageEvents(all), [all])
  const openImage = useCallback(
    (segmentId: string) => {
      const index = images.findIndex((image) => image.segment_id === segmentId)
      if (index >= 0) setLightboxIndex(index)
    },
    [images],
  )
  const activeId = useMemo(() => {
    if (playheadMs === undefined) return null
    const hit = events.find(
      (e): e is TranscriptEvent =>
        e.type === "transcript" && playheadMs >= e.start_ms && playheadMs < e.end_ms,
    )
    return hit ? hit.artifact_id + hit.start_ms : null
  }, [playheadMs, events])
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
        : events.filter((e): e is VideoClipEvent => e.type === "video-clip"),
    [events, hidden],
  )
  const openClip = useCallback(
    (clip: VideoClipEvent, offsetMs: number) => setPlaying({ clip, offsetMs }),
    [],
  )
  useEffect(() => {
    if (clips.length === 0) setGutterW(0)
  }, [clips.length])

  useEffect(() => {
    if (!hasMore) onDone?.()
  }, [hasMore, onDone])

  // The next chunk renders as the end of the list nears.
  const moreRef = useRef<HTMLDivElement | null>(null)
  useEffect(() => {
    const node = moreRef.current
    if (!node || !hasMore) return
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) setLimit((n) => n + CHUNK)
      },
      { rootMargin: "800px" },
    )
    observer.observe(node)
    return () => observer.disconnect()
  }, [hasMore, limit])

  const started = Date.parse(startedAt)
  const stamp = (ms: number) => fmtWallTime(new Date(started + ms).toISOString())

  // Flash-and-scroll the focus event once.
  useEffect(() => {
    if (focusIndex < 0 || flashed.current) return
    flashed.current = true
    setFlashKey(eventKey(all[focusIndex]!, focusIndex))
  }, [all, focusIndex])

  const flashRef = useCallback((node: HTMLDivElement | null) => {
    node?.scrollIntoView({ block: "center", behavior: "smooth" })
  }, [])

  const saveEdit = async (event: TranscriptEvent) => {
    const text = editDraft.trim()
    if (!text || text === (edits.get(event.artifact_id) ?? event.text)) {
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
    setEdits((prev) => new Map(prev).set(event.artifact_id, text))
    setEditingKey(null)
    onEdited?.()
  }

  // Follow mode: as playback crosses into a new utterance, nudge its row
  // into view. block:"nearest" keeps this gentle — no scroll when visible.
  useEffect(() => {
    if (!follow || !activeId) return
    document
      .querySelector(".event.playing")
      ?.scrollIntoView({ block: "nearest", behavior: "smooth" })
  }, [follow, activeId])

  const rendered = events.map((event, index) => {
    const key = eventKey(event, index)
    const flash = key === flashKey
    switch (event.type) {
      case "transcript": {
        if (hidden.has("transcript")) return null
        const text = edits.get(event.artifact_id) ?? event.text
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
              {stamp(event.at_ms)}
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
                  <span className="transcript-text">{text}</span>
                  <button
                    className="btn ghost slim event-edit"
                    title="Correct this transcript"
                    onClick={() => {
                      setEditDraft(text)
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
              {stamp(event.at_ms)}
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
              {stamp(event.at_ms)}
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
              {stamp(event.at_ms)}
            </button>
            <div className="event-body chips">
              <span className="chip strong" title={describeSpan(event)}>
                {event.label}
              </span>
              <span className="row-dim">
                {stamp(event.start_ms)}–{stamp(event.end_ms)} ·{" "}
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
              {stamp(Math.max(0, first.at_ms))}
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
                  {stamp(Math.max(0, first.at_ms))}–{stamp(Math.max(0, last.at_ms))}
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
                        {stamp(Math.max(0, point.at_ms))}
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
              {stamp(at)}
            </button>
            <div className="event-body gallery">
              {run.map((image) => (
                <button
                  key={image.segment_id}
                  className="thumb"
                  title={`${fmtWallTime(image.captured_at)} · open`}
                  onClick={() => openImage(image.segment_id)}
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
      case "session-end":
        // Recording boundaries are drawn by the page, between recordings.
        return null
      default:
        // speech-start/speech-end and any future event types: not rendered.
        return null
    }
  })

  // Rows carry their position so the video gutter can line clips up with them.
  const tagged = rendered.map((node, index) =>
    node ? cloneElement(node as ReactElement<{ "data-at"?: number }>, { "data-at": events[index]!.at_ms }) : null,
  )

  return (
    <>
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
          {tagged}
          {hasMore && <div ref={moreRef} className="timeline-more" />}
        </div>
      </div>
      {lightboxIndex !== null && (
        <ImageLightbox
          images={images}
          index={lightboxIndex}
          onIndex={setLightboxIndex}
          onClose={() => setLightboxIndex(null)}
          onSeek={onSeek}
        />
      )}
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
