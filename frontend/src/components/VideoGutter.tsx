import { useEffect, useLayoutEffect, useRef, useState, type RefObject } from "react"
import type { VideoClipEvent } from "../api/client"
import { fmtClock } from "../format"
import { artifactUrl, fmtWallTime } from "../visual"

export const FILM_W = 64
// Each card behind shows at least this much, so a longer clip (more frames)
// always stacks further down even when few rows fall inside it.
const MIN_PEEK = 6
const LANE_GAP = 6

type Placed = { clip: VideoClipEvent; top: number; height: number; lane: number; frameH: number }

function frameHeight(clip: VideoClipEvent): number {
  const first = clip.frames[0]
  const ratio =
    first?.width && first.height
      ? first.height / first.width
      : clip.width && clip.height
        ? clip.height / clip.width
        : 9 / 16
  return Math.round(FILM_W * ratio)
}

// Pixel extent of each clip against the rendered rows (`[data-at]` children of
// `.timeline`): from the first row inside [at_ms, end_ms) to the last one.
function place(wrap: HTMLElement, clips: VideoClipEvent[]): Placed[] {
  const base = wrap.getBoundingClientRect().top
  const rows = [...wrap.querySelectorAll<HTMLElement>(".timeline > [data-at]")].map((el) => {
    const r = el.getBoundingClientRect()
    return { at: Number(el.dataset.at), top: r.top - base, bottom: r.bottom - base }
  })
  const listBottom = rows.length ? rows[rows.length - 1]!.bottom : 0
  const laneEnds: number[] = []
  return clips.map((clip) => {
    const inside = rows.filter((r) => r.at >= clip.at_ms && r.at < clip.end_ms)
    const frameH = frameHeight(clip)
    const top = inside.length
      ? inside[0]!.top
      : (rows.find((r) => r.at >= clip.at_ms)?.top ?? listBottom)
    const stackH = frameH + (Math.max(1, clip.frames.length) - 1) * MIN_PEEK
    const bottom = Math.max(inside.length ? inside[inside.length - 1]!.bottom : top, top + stackH)
    let lane = laneEnds.findIndex((end) => end <= top)
    if (lane < 0) lane = laneEnds.length
    laneEnds[lane] = bottom + 4
    return { clip, top, height: bottom - top, lane, frameH }
  })
}

/** Clips as filmstrips in a left gutter, overlapping the rows they span. */
export default function VideoGutter({
  clips,
  wrapRef,
  token,
  onOpen,
  onMediaError,
  onWidth,
}: {
  clips: VideoClipEvent[]
  wrapRef: RefObject<HTMLDivElement | null>
  token: string | null
  onOpen: (clip: VideoClipEvent, offsetMs: number) => void
  onMediaError: () => void
  onWidth: (px: number) => void
}) {
  const [placed, setPlaced] = useState<Placed[]>([])
  const signature = useRef("")

  const measure = () => {
    const wrap = wrapRef.current
    const next = wrap && clips.length ? place(wrap, clips) : []
    const sig = next.map((p) => `${p.clip.segment_id}:${p.top}:${p.height}:${p.lane}`).join("|")
    if (sig === signature.current) return
    signature.current = sig
    setPlaced(next)
    const lanes = next.reduce((n, p) => Math.max(n, p.lane + 1), 0)
    onWidth(lanes ? lanes * (FILM_W + LANE_GAP) : 0)
  }

  // Every render may move rows (pages, edits, expansions); the signature
  // check keeps this from looping.
  useLayoutEffect(measure)

  useEffect(() => {
    const wrap = wrapRef.current
    if (!wrap) return
    const observer = new ResizeObserver(() => measure())
    observer.observe(wrap)
    return () => observer.disconnect()
  })

  return (
    <div className="video-gutter">
      {placed.map((p) => (
        <Filmstrip
          key={p.clip.segment_id}
          placed={p}
          token={token}
          onOpen={onOpen}
          onMediaError={onMediaError}
        />
      ))}
    </div>
  )
}

function Filmstrip({
  placed: { clip, top, height, lane, frameH },
  token,
  onOpen,
  onMediaError,
}: {
  placed: Placed
  token: string | null
  onOpen: (clip: VideoClipEvent, offsetMs: number) => void
  onMediaError: () => void
}) {
  const durationMs = Math.max(0, clip.end_ms - clip.at_ms)
  const frames = clip.frames
  // Until the thumbnail pipeline runs: one blank card that still opens the clip.
  const total = Math.max(1, frames.length)
  const fits = 1 + Math.floor(Math.max(0, height - frameH) / MIN_PEEK)
  const shown = Math.min(total, fits)
  const picks = Array.from({ length: shown }, (_, j) =>
    shown === 1 ? 0 : Math.round((j / (shown - 1)) * (total - 1)),
  )
  // Cards fan down to fill the clip's span, never leaving gaps between them.
  const peek = shown > 1 ? Math.min(frameH, (height - frameH) / (shown - 1)) : 0

  return (
    <div
      className="film"
      style={{ top, height, left: lane * (FILM_W + LANE_GAP), width: FILM_W }}
      title={`video · ${fmtWallTime(clip.captured_at)} · ${fmtClock(durationMs)}`}
    >
      {picks.map((i, j) => {
        const frame = frames[i]
        const offsetMs = frame?.offset_ms ?? 0
        const depth = Math.min(j, 8)
        return (
          <button
            key={j}
            className="film-card"
            style={{
              top: j * peek,
              height: frameH,
              zIndex: shown - j,
              transform: `scale(${1 - depth * 0.004})`,
              filter: `brightness(${1 - depth * 0.02})`,
            }}
            title={`${fmtClock(Math.max(0, clip.at_ms) + offsetMs)} · play from here`}
            onClick={() => onOpen(clip, offsetMs)}
          >
            {frame && token && (
              <img src={artifactUrl(frame.artifact_id, token)} alt="" onError={onMediaError} />
            )}
          </button>
        )
      })}
    </div>
  )
}
