import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import type { SoundSpanEvent, TimelineEvent, TranscriptEvent } from "../api/client"
import { fmtClock } from "../format"
import { usePlayhead } from "../hooks/usePlayhead"
import SpeakerChip from "./SpeakerChip"
import TimelineStrip from "./TimelineStrip"

const DURATION_S = 64

// Ids are chosen so voices (teal, umber, violet) never share a colour with the sounds.
const PEOPLE = {
  maya: { id: "demo-maya-0", name: "Maya" },
  jonah: { id: "demo-jonah-4", name: "Jonah" },
  priya: { id: "demo-priya-1", name: "Priya" },
}

const SCRIPT: [keyof typeof PEOPLE, number, number, string][] = [
  ["maya", 0.5, 5.5, "Lorem ipsum dolor sit amet, consectetur adipiscing elit."],
  ["jonah", 6, 10, "Sed do eiusmod tempor incididunt ut labore."],
  ["maya", 10.5, 15, "Ut enim ad minim veniam, quis nostrud exercitation."],
  ["priya", 15.5, 18, "Duis aute irure dolor."],
  ["jonah", 18.5, 24, "In reprehenderit in voluptate velit esse cillum dolore eu fugiat."],
  ["maya", 24.5, 28, "Excepteur sint occaecat cupidatat non proident."],
  ["priya", 29, 35, "Sunt in culpa qui officia deserunt mollit anim id est laborum."],
  ["jonah", 35.5, 39, "Curabitur pretium tincidunt lacus."],
  ["maya", 39.5, 44.5, "Nulla gravida orci a odio, nullam varius turpis et commodo."],
  ["priya", 45, 48, "Pharetra est, eros bibendum elit."],
  ["jonah", 48.5, 54, "Nec luctus magna felis sollicitudin mauris integer in mauris."],
  ["maya", 54.5, 58, "Eu tellus, sed convallis tristique."],
  ["priya", 58.5, 63, "Fusce dapibus tellus ac cursus commodo."],
]

const EVENTS: TranscriptEvent[] = SCRIPT.map(([who, start, end, text], i) => ({
  type: "transcript",
  artifact_id: `demo-${i}`,
  at_ms: start * 1000,
  start_ms: start * 1000,
  end_ms: end * 1000,
  text,
  chars: text.length,
  speaker: who,
  speaker_id: PEOPLE[who].id,
  speaker_name: PEOPLE[who].name,
}))

// A worksite soundscape in AudioSet labels: [label, start s, end s, peak, mean].
const SOUNDS: [string, number, number, number, number][] = [
  ["Sawing", 3, 13, 0.86, 0.66],
  ["Sawing", 33, 41, 0.92, 0.74],
  ["Sawing", 52, 60, 0.8, 0.58],
  ["Wind noise (microphone)", 0, 22, 0.58, 0.4],
  ["Wind noise (microphone)", 27, 64, 0.52, 0.36],
  ["Drill", 16, 19, 0.74, 0.55],
  ["Drill", 45, 49, 0.81, 0.62],
]

const SPANS: SoundSpanEvent[] = SOUNDS.map(([label, start, end, peak, mean], i) => ({
  type: "sound-span",
  artifact_id: `demo-sound-${i}`,
  at_ms: start * 1000,
  start_ms: start * 1000,
  end_ms: end * 1000,
  label,
  peak,
  mean,
  windows: Math.round(end - start),
}))

const STRIP_EVENTS: TimelineEvent[] = [...EVENTS, ...SPANS]

/** A silent 8 kHz WAV: a real media clock for the strip's playhead, no network. */
function silentWavUrl(seconds: number): string {
  const rate = 8000
  const n = rate * seconds
  const buf = new ArrayBuffer(44 + n)
  const v = new DataView(buf)
  const str = (o: number, s: string) => [...s].forEach((c, i) => v.setUint8(o + i, c.charCodeAt(0)))
  str(0, "RIFF")
  v.setUint32(4, 36 + n, true)
  str(8, "WAVEfmt ")
  v.setUint32(16, 16, true)
  v.setUint16(20, 1, true)
  v.setUint16(22, 1, true)
  v.setUint32(24, rate, true)
  v.setUint32(28, rate, true)
  v.setUint16(32, 1, true)
  v.setUint16(34, 8, true)
  str(36, "data")
  v.setUint32(40, n, true)
  new Uint8Array(buf, 44).fill(128)
  return URL.createObjectURL(new Blob([buf], { type: "audio/wav" }))
}

/** An interactive moment window built from the real strip and transcript rows. */
export default function HomeDemo() {
  const [audioEl, setAudioEl] = useState<HTMLAudioElement | null>(null)
  const [src, setSrc] = useState<string | null>(null)
  const [follow, setFollow] = useState(true)
  const listRef = useRef<HTMLDivElement | null>(null)
  const head = usePlayhead(audioEl, "coarse")

  useEffect(() => {
    const url = silentWavUrl(DURATION_S)
    setSrc(url)
    return () => URL.revokeObjectURL(url)
  }, [])

  const seek = useCallback(
    (ms: number, play: boolean) => {
      if (!audioEl) return
      audioEl.currentTime = ms / 1000
      if (play) void audioEl.play().catch(() => {})
    },
    [audioEl],
  )

  const activeId = useMemo(() => {
    const e = EVENTS.find((e) => head.ms >= e.start_ms && head.ms < e.end_ms)
    return e ? e.artifact_id : null
  }, [head.ms])

  // Scroll only the list, never the page.
  useEffect(() => {
    const list = listRef.current
    if (!follow || !activeId || !list) return
    const row = list.querySelector<HTMLElement>(".event.playing")
    if (!row) return
    const top = row.getBoundingClientRect().top - list.getBoundingClientRect().top + list.scrollTop
    if (top < list.scrollTop || top + row.offsetHeight > list.scrollTop + list.clientHeight)
      list.scrollTo({ top: top - list.clientHeight / 3, behavior: "smooth" })
  }, [follow, activeId])

  return (
    <div className="demo">
      <div className="demo-head">
        <button
          className="btn slim"
          onClick={() => (head.playing ? audioEl?.pause() : void audioEl?.play().catch(() => {}))}
        >
          {head.playing ? "Pause" : "Play"}
        </button>
        <span className="demo-title">Tue 14:02</span>
        <span className="demo-clock">
          {fmtClock(head.ms)} / {fmtClock(DURATION_S * 1000)}
        </span>
        {src && <audio ref={setAudioEl} src={src} preload="auto" muted />}
      </div>
      <TimelineStrip
        events={STRIP_EVENTS}
        truncated={false}
        audioEl={audioEl}
        follow={follow}
        onFollowChange={setFollow}
        onSeek={seek}
      />
      <div className="demo-list" ref={listRef}>
        <div className="demo-rail">
          {EVENTS.map((e) => (
            <div
              key={e.artifact_id}
              className={`event transcript ${activeId === e.artifact_id ? "playing" : ""}`}
            >
              <button className="event-time" title="Listen from here" onClick={() => seek(e.at_ms, true)}>
                {fmtClock(e.at_ms)}
              </button>
              <div className="event-body">
                <SpeakerChip clusterId={e.speaker_id} name={e.speaker_name} />
                <span className="transcript-text">{e.text}</span>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
