import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type RefObject,
} from "react"
import type { AudioTagEvent, TimelineEvent, TranscriptEvent } from "../api/client"
import { fmtClock } from "../format"
import { usePlayhead } from "../hooks/usePlayhead"
import { describePoint, locationPoints } from "../location"
import { hue, localLabelTail } from "../speakers"
import {
  SOUND_LANES_SHOWN,
  buildSoundLanes,
  describeSpan,
  soundAlpha,
  soundBorderAlpha,
} from "../sounds"
import SpeakerChip from "./SpeakerChip"

// The visual session timeline: one lane per voice, utterance blocks placed at
// [start_ms, end_ms), one lane per sound class beneath them, a smooth playhead
// riding the main <audio> element. Click anywhere to listen there; drag to
// scrub; ctrl/pinch-wheel zooms around the cursor; plain wheel pans when
// zoomed. The block/ruler DOM is memoized — while playing, only the playhead
// overlay re-renders per frame.

const RULER_H = 24
const LANE_H = 28
const TICK_TARGET_PX = 90
const TICK_STEPS_MS = [
  1000, 2000, 5000, 10_000, 15_000, 30_000, 60_000, 120_000, 300_000, 600_000,
  900_000, 1_800_000, 3_600_000,
]

type Lane = {
  key: string
  clusterId: string | null
  name: string | null
  localLabel: string | null
  // Voice-prints behind this lane (one per diarization block) — what the
  // chip's "just this voice" reassign moves.
  memberIds: string[]
  talkMs: number
  blocks: TranscriptEvent[]
}

type Tooltip = {
  x: number
  y: number
  color: string | null
  title: string
  time: string
  text: string | null
}

function buildLanes(events: TimelineEvent[]): Lane[] {
  const lanes = new Map<string, Lane>()
  for (const event of events) {
    if (event.type !== "transcript") continue
    const key = event.speaker_id ?? (event.speaker ? `local:${event.speaker}` : "unknown")
    let lane = lanes.get(key)
    if (!lane) {
      lane = {
        key,
        clusterId: event.speaker_id ?? null,
        name: null,
        localLabel: null,
        memberIds: [],
        talkMs: 0,
        blocks: [],
      }
      lanes.set(key, lane)
    }
    lane.name ??= event.speaker_name ?? null
    lane.localLabel ??= event.speaker ?? null
    if (event.voiceprint_id && !lane.memberIds.includes(event.voiceprint_id))
      lane.memberIds.push(event.voiceprint_id)
    lane.talkMs += Math.max(0, event.end_ms - event.start_ms)
    lane.blocks.push(event)
  }
  return [...lanes.values()].sort((a, b) => b.talkMs - a.talkMs)
}

// The element's duration without subscribing to per-frame position updates.
function useAudioDuration(audio: HTMLAudioElement | null): number | null {
  const [durationMs, setDurationMs] = useState<number | null>(null)
  useEffect(() => {
    if (!audio) return
    const read = () =>
      setDurationMs(Number.isFinite(audio.duration) ? audio.duration * 1000 : null)
    read()
    audio.addEventListener("loadedmetadata", read)
    audio.addEventListener("durationchange", read)
    return () => {
      audio.removeEventListener("loadedmetadata", read)
      audio.removeEventListener("durationchange", read)
    }
  }, [audio])
  return durationMs
}

export default function TimelineStrip({
  events,
  truncated,
  audioEl,
  follow,
  onFollowChange,
  onSeek,
  onSpeakersChanged,
  mapOpen,
  onMapToggle,
}: {
  events: TimelineEvent[]
  truncated: boolean
  audioEl: HTMLAudioElement | null
  follow: boolean
  onFollowChange: (follow: boolean) => void
  onSeek: (ms: number, play: boolean) => void
  onSpeakersChanged: () => void
  mapOpen?: boolean
  onMapToggle?: () => void
}) {
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const [zoom, setZoom] = useState(1)
  const [viewportW, setViewportW] = useState(0)
  const [tooltip, setTooltip] = useState<Tooltip | null>(null)
  const scrubbing = useRef(false)
  const seekFrame = useRef(0)
  const programmaticScroll = useRef(false)
  const userScrollUntil = useRef(0)

  const [showAllSounds, setShowAllSounds] = useState(false)

  const lanes = useMemo(() => buildLanes(events), [events])
  const tagBlocks = useMemo(
    () =>
      events.filter(
        (e): e is AudioTagEvent => e.type === "audio-tag" && e.labels.length > 0,
      ),
    [events],
  )
  const soundLanes = useMemo(() => buildSoundLanes(events), [events])
  const points = useMemo(() => locationPoints(events), [events])
  const visibleSoundLanes = useMemo(
    () => (showAllSounds ? soundLanes : soundLanes.slice(0, SOUND_LANES_SHOWN)),
    [soundLanes, showAllSounds],
  )
  // The raw ~10s tag lane is what the span lanes replace. Keep it only for
  // sessions the span tier hasn't reached, so nothing regresses to blank.
  const showTagLane = soundLanes.length === 0 && tagBlocks.length > 0

  const audioDurationMs = useAudioDuration(audioEl)
  const durationMs = useMemo(() => {
    let end = audioDurationMs ?? 0
    for (const event of events) {
      end = Math.max(end, event.at_ms, "end_ms" in event ? event.end_ms : 0)
    }
    return end
  }, [events, audioDurationMs])

  const maxZoom = Math.min(64, Math.max(4, Math.round(durationMs / 15_000)))

  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    const observer = new ResizeObserver(() => setViewportW(el.clientWidth))
    observer.observe(el)
    return () => observer.disconnect()
  }, [])

  // React registers wheel listeners passively; zoom needs preventDefault, so
  // the listener is attached natively.
  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    const onWheel = (e: WheelEvent) => {
      if (e.ctrlKey || e.metaKey) {
        e.preventDefault()
        const rect = el.getBoundingClientRect()
        const cursor = e.clientX - rect.left
        const frac = (cursor + el.scrollLeft) / Math.max(1, el.scrollWidth)
        setZoom((z) => {
          const next = Math.min(maxZoom, Math.max(1, z * Math.exp(-e.deltaY * 0.002)))
          const target = frac * el.clientWidth * next - cursor
          requestAnimationFrame(() => {
            programmaticScroll.current = true
            el.scrollLeft = Math.max(0, target)
          })
          return next
        })
      } else if (
        Math.abs(e.deltaY) > Math.abs(e.deltaX) &&
        el.scrollWidth > el.clientWidth
      ) {
        e.preventDefault()
        el.scrollLeft += e.deltaY
      }
    }
    el.addEventListener("wheel", onWheel, { passive: false })
    return () => el.removeEventListener("wheel", onWheel)
  }, [maxZoom])

  const timeAt = useCallback(
    (clientX: number): number => {
      const el = scrollRef.current
      if (!el || durationMs <= 0) return 0
      const rect = el.getBoundingClientRect()
      const x = clientX - rect.left + el.scrollLeft
      return Math.max(0, Math.min(durationMs, (x / Math.max(1, el.scrollWidth)) * durationMs))
    },
    [durationMs],
  )

  const showBlockTip = useCallback((e: React.PointerEvent, tip: Omit<Tooltip, "x" | "y">) => {
    const host = scrollRef.current?.parentElement
    if (!host) return
    const rect = host.getBoundingClientRect()
    setTooltip({
      ...tip,
      x: Math.min(rect.width - 30, Math.max(10, e.clientX - rect.left)),
      y: e.clientY - rect.top,
    })
  }, [])
  const hideTip = useCallback(() => setTooltip(null), [])

  // Memoized heavy DOM: ruler ticks, lane blocks, tag lane. Re-renders only
  // when the data or geometry changes — never per playhead frame.
  const canvasContent = useMemo(() => {
    if (durationMs <= 0) return null
    const pxPerMs = (viewportW * zoom) / durationMs
    const step =
      TICK_STEPS_MS.find((s) => s * pxPerMs >= TICK_TARGET_PX) ??
      TICK_STEPS_MS[TICK_STEPS_MS.length - 1]!
    const ticks: number[] = []
    for (let t = step; t < durationMs; t += step) ticks.push(t)

    return (
      <>
        <div className="strip-ruler" style={{ height: RULER_H }}>
          {ticks.map((t) => (
            <span key={t} className="strip-tick" style={{ left: `${(t / durationMs) * 100}%` }}>
              {fmtClock(t)}
            </span>
          ))}
        </div>
        {lanes.map((lane) => {
          const color = hue(lane.clusterId ?? lane.localLabel ?? "?")
          const laneName =
            lane.name ??
            (lane.localLabel ? localLabelTail(lane.localLabel) : "unknown voice")
          return (
            <div key={lane.key} className="strip-lane" style={{ height: LANE_H }}>
              {lane.blocks.map((block) => (
                <div
                  key={block.artifact_id + block.start_ms}
                  className={`strip-block ${lane.clusterId && lane.name ? "" : "unresolved"}`}
                  style={{
                    left: `${(block.start_ms / durationMs) * 100}%`,
                    width: `${(Math.max(200, block.end_ms - block.start_ms) / durationMs) * 100}%`,
                    background: `hsl(${color} 55% 55% / 0.55)`,
                    borderColor: `hsl(${color} 60% 62% / 0.9)`,
                  }}
                  onClick={(e) => {
                    e.stopPropagation()
                    onSeek(block.start_ms, true)
                  }}
                  onPointerMove={(e) =>
                    showBlockTip(e, {
                      color: `hsl(${color} 62% 58%)`,
                      title: laneName,
                      time: `${fmtClock(block.start_ms)}–${fmtClock(block.end_ms)}`,
                      text: block.text,
                    })
                  }
                  onPointerLeave={hideTip}
                />
              ))}
            </div>
          )
        })}
        {visibleSoundLanes.map((lane, i) => {
          const color = hue(lane.label)
          const last = i === visibleSoundLanes.length - 1
          return (
            <div
              key={`sound:${lane.label}`}
              className={`strip-lane sound${i === 0 ? " sound-first" : ""}${
                last ? " sound-last" : ""
              }`}
              style={{ height: LANE_H }}
            >
              {lane.spans.map((span) => (
                <div
                  key={span.artifact_id}
                  className="strip-block sound"
                  style={{
                    left: `${(span.start_ms / durationMs) * 100}%`,
                    width: `${
                      (Math.max(200, span.end_ms - span.start_ms) / durationMs) * 100
                    }%`,
                    background: `hsl(${color} 55% 55% / ${soundAlpha(span)})`,
                    borderColor: `hsl(${color} 60% 62% / ${soundBorderAlpha(span)})`,
                  }}
                  onClick={(e) => {
                    e.stopPropagation()
                    onSeek(span.start_ms, true)
                  }}
                  onPointerMove={(e) =>
                    showBlockTip(e, {
                      color: `hsl(${color} 62% 58%)`,
                      title: lane.label,
                      time: `${fmtClock(span.start_ms)}–${fmtClock(span.end_ms)}`,
                      text: describeSpan(span),
                    })
                  }
                  onPointerLeave={hideTip}
                />
              ))}
            </div>
          )
        })}
        {showTagLane && (
          <div className="strip-lane tags" style={{ height: LANE_H }}>
            {tagBlocks.map((block) => (
              <div
                key={block.artifact_id + block.start_ms}
                className="strip-block tag"
                style={{
                  left: `${(block.start_ms / durationMs) * 100}%`,
                  width: `${(Math.max(200, block.end_ms - block.start_ms) / durationMs) * 100}%`,
                }}
                onClick={(e) => {
                  e.stopPropagation()
                  onSeek(block.start_ms, true)
                }}
                onPointerMove={(e) =>
                  showBlockTip(e, {
                    color: null,
                    title: block.labels.map((l) => l.label).join(" · "),
                    time: `${fmtClock(block.start_ms)}–${fmtClock(block.end_ms)}`,
                    text: null,
                  })
                }
                onPointerLeave={hideTip}
              />
            ))}
          </div>
        )}
        {points.length > 0 && (
          <div className="strip-lane location" style={{ height: LANE_H }}>
            {points.map((point, i) => (
              <div
                key={`${point.segment_id}-${point.at_ms}-${i}`}
                className="strip-block location"
                style={{
                  left: `${(Math.max(0, point.at_ms) / durationMs) * 100}%`,
                  width: 2,
                }}
                onClick={(e) => {
                  e.stopPropagation()
                  onSeek(Math.max(0, point.at_ms), true)
                }}
                onPointerMove={(e) =>
                  showBlockTip(e, {
                    color: null,
                    title: "location",
                    time: fmtClock(Math.max(0, point.at_ms)),
                    text: describePoint(point),
                  })
                }
                onPointerLeave={hideTip}
              />
            ))}
          </div>
        )}
      </>
    )
  }, [
    lanes,
    visibleSoundLanes,
    showTagLane,
    tagBlocks,
    points,
    durationMs,
    viewportW,
    zoom,
    onSeek,
    showBlockTip,
    hideTip,
  ])

  if (
    durationMs <= 0 ||
    (lanes.length === 0 &&
      soundLanes.length === 0 &&
      tagBlocks.length === 0 &&
      points.length === 0)
  )
    return null

  const canvasH =
    RULER_H +
    LANE_H *
      (lanes.length +
        visibleSoundLanes.length +
        (showTagLane ? 1 : 0) +
        (points.length > 0 ? 1 : 0))

  return (
    <div className="strip">
      <div className="strip-toolbar">
        <span className="strip-title">
          {lanes.length} voice{lanes.length === 1 ? "" : "s"}
          {soundLanes.length > 0 &&
            ` · ${soundLanes.length} sound${soundLanes.length === 1 ? "" : "s"}`}
          {truncated && (
            <span className="row-dim"> · long session — strip shows the first part</span>
          )}
        </span>
        {soundLanes.length > SOUND_LANES_SHOWN && (
          <button
            className="chip as-button"
            title="show every sound class, not just the most confident"
            onClick={() => setShowAllSounds((v) => !v)}
          >
            {showAllSounds
              ? `top ${SOUND_LANES_SHOWN} sounds`
              : `+${soundLanes.length - SOUND_LANES_SHOWN} more sounds`}
          </button>
        )}
        {onMapToggle && (points.length > 0 || truncated) && (
          <button
            className={`chip as-button ${mapOpen ? "strong" : ""}`}
            title="show where you were on the session's timeline"
            onClick={onMapToggle}
          >
            {mapOpen ? "hide map" : "map"}
          </button>
        )}
        <button
          className={`chip as-button ${follow ? "strong" : ""}`}
          title="keep the playhead and transcript in view while playing"
          onClick={() => onFollowChange(!follow)}
        >
          {follow ? "following" : "follow"}
        </button>
        <div className="strip-zoom">
          <button
            className="btn ghost slim"
            title="zoom out"
            onClick={() => setZoom((z) => Math.max(1, z / 1.5))}
          >
            −
          </button>
          <button
            className="btn ghost slim"
            title="zoom in (or ctrl+scroll on the strip)"
            onClick={() => setZoom((z) => Math.min(maxZoom, z * 1.5))}
          >
            +
          </button>
          <button
            className="btn ghost slim"
            title="fit the whole session"
            onClick={() => {
              setZoom(1)
              if (scrollRef.current) scrollRef.current.scrollLeft = 0
            }}
          >
            fit
          </button>
        </div>
      </div>

      <div className="strip-body">
        <div className="strip-heads" style={{ paddingTop: RULER_H }}>
          {lanes.map((lane) => (
            <div key={lane.key} className="strip-head" style={{ height: LANE_H }}>
              <SpeakerChip
                clusterId={lane.clusterId}
                name={lane.name}
                localLabel={lane.localLabel}
                memberIds={lane.memberIds}
                onChanged={onSpeakersChanged}
              />
              <span className="strip-talk">{fmtClock(lane.talkMs)}</span>
            </div>
          ))}
          {visibleSoundLanes.map((lane, i) => (
            <div
              key={`sound:${lane.label}`}
              className={`strip-head${i === 0 ? " sound-first" : ""}`}
              style={{ height: LANE_H }}
            >
              <span
                className="strip-head-sound"
                title={`${lane.label} · ${fmtClock(lane.coveredMs)} covered`}
              >
                <span
                  className="speaker-dot"
                  style={{ background: `hsl(${hue(lane.label)} 62% 58%)` }}
                />
                {lane.label}
              </span>
              <span className="strip-talk">{lane.peak.toFixed(2)}</span>
            </div>
          ))}
          {showTagLane && (
            <div className="strip-head" style={{ height: LANE_H }}>
              <span className="strip-head-tags">sounds</span>
            </div>
          )}
          {points.length > 0 && (
            <div className="strip-head" style={{ height: LANE_H }}>
              <span className="strip-head-sound" title={`${points.length} location points`}>
                <span className="speaker-dot" style={{ background: "hsl(200 62% 58%)" }} />
                location
              </span>
              <span className="strip-talk">{points.length}</span>
            </div>
          )}
        </div>

        <div
          className="strip-scroll"
          ref={scrollRef}
          onScroll={() => {
            if (programmaticScroll.current) programmaticScroll.current = false
            else userScrollUntil.current = Date.now() + 2500
          }}
          onPointerDown={(e) => {
            if (e.button !== 0) return
            if ((e.target as HTMLElement).closest(".strip-block")) return
            scrubbing.current = true
            e.currentTarget.setPointerCapture(e.pointerId)
            onSeek(timeAt(e.clientX), false)
          }}
          onPointerMove={(e) => {
            if (!scrubbing.current) return
            const x = e.clientX
            cancelAnimationFrame(seekFrame.current)
            seekFrame.current = requestAnimationFrame(() => onSeek(timeAt(x), false))
          }}
          onPointerUp={(e) => {
            if (!scrubbing.current) return
            scrubbing.current = false
            cancelAnimationFrame(seekFrame.current)
            onSeek(timeAt(e.clientX), true)
          }}
          onPointerCancel={() => {
            scrubbing.current = false
          }}
        >
          <div
            className="strip-canvas"
            style={{ width: `${zoom * 100}%`, height: canvasH }}
          >
            {canvasContent}
            <PlayheadOverlay
              audio={audioEl}
              durationMs={durationMs}
              follow={follow}
              scrollRef={scrollRef}
              programmaticScroll={programmaticScroll}
              userScrollUntil={userScrollUntil}
            />
          </div>
        </div>
        {tooltip && (
          <div className="strip-tip" style={{ left: tooltip.x, top: tooltip.y }}>
            <div className="strip-tip-head">
              {tooltip.color && (
                <span className="speaker-dot" style={{ background: tooltip.color }} />
              )}
              <strong>{tooltip.title}</strong>
              <span className="row-dim">{tooltip.time}</span>
            </div>
            {tooltip.text && <div className="strip-tip-text">{tooltip.text}</div>}
          </div>
        )}
      </div>
    </div>
  )
}

// The only per-frame subscriber: a vertical line plus a time bubble. While
// playing (and follow is on) it keeps itself inside the visible window,
// yielding to any scroll the user made in the last couple of seconds.
function PlayheadOverlay({
  audio,
  durationMs,
  follow,
  scrollRef,
  programmaticScroll,
  userScrollUntil,
}: {
  audio: HTMLAudioElement | null
  durationMs: number
  follow: boolean
  scrollRef: RefObject<HTMLDivElement | null>
  programmaticScroll: RefObject<boolean>
  userScrollUntil: RefObject<number>
}) {
  const head = usePlayhead(audio, "smooth")

  useEffect(() => {
    if (!follow || !head.playing) return
    const el = scrollRef.current
    if (!el || el.scrollWidth <= el.clientWidth + 1) return
    if (Date.now() < userScrollUntil.current) return
    const px = (head.ms / durationMs) * el.scrollWidth
    if (px < el.scrollLeft + 24 || px > el.scrollLeft + el.clientWidth - 48) {
      programmaticScroll.current = true
      el.scrollLeft = Math.max(0, px - el.clientWidth * 0.25)
    }
  }, [head.ms, head.playing, follow, durationMs, scrollRef, programmaticScroll, userScrollUntil])

  if (durationMs <= 0) return null
  const frac = Math.max(0, Math.min(1, head.ms / durationMs))
  return (
    <div className="strip-playhead" style={{ left: `${frac * 100}%` }}>
      <span className="strip-playhead-time">{fmtClock(head.ms)}</span>
    </div>
  )
}
