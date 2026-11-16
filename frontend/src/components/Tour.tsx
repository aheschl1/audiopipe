import { type CSSProperties, useEffect, useLayoutEffect, useRef, useState } from "react"
import { useLocation, useNavigate } from "react-router"
import { api } from "../api/client"

type Step = {
  // Where the step lives; a function runs on arrival, so it can read the page it leaves.
  path?: string | (() => string | null | undefined | Promise<string | null | undefined>)
  // First selector that matches is the anchor; none (or no match) centres the bubble.
  anchor?: string[]
  text: string
  // The visitor got there on their own: move on.
  done?: (path: string) => boolean
}

const STEPS: Step[] = [
  {
    path: "/",
    anchor: ["a.moment"],
    text: "This is a moment: a drive-through oil change, put together from what the glasses heard and saw.",
    done: (path) => path.startsWith("/moments/"),
  },
  {
    path: () => document.querySelector("a.moment")?.getAttribute("href"),
    anchor: [".moment-head-summary", ".moment-head-title"],
    text: "The title and summary are written from the transcript and the video frames.",
  },
  {
    anchor: [".facts"],
    text: "Who spoke, what came up, and text seen in frame. Named voices carry across recordings.",
  },
  {
    anchor: [".moment-automations"],
    text: "An automation pulled out the details: the complaint, the torn CV boot, and the quote.",
  },
  {
    anchor: [".evidence"],
    text: "Everything behind it, in time order: what was heard, and what was seen.",
  },
  {
    anchor: [".ev-aside"],
    text: "Connections tie the two together, like the clicking complaint and the footage of the boot. Hover a line to see its links.",
  },
  { path: "/search", anchor: [".search-bar"], text: "Search everything by what was said or what was seen." },
  { path: "/speakers", anchor: [".speaker-row", ".list"], text: "People: every voice, recognized across recordings." },
  {
    path: async () => {
      const { data } = await api.GET("/v1/sessions", { params: { query: { limit: 1, offset: 0 } } })
      const id = data?.items[0]?.id
      return id ? `/sessions/${id}` : "/sessions"
    },
    anchor: [".timeline-wrap", ".session-main"],
    text: "Recordings: the raw timeline, for detailed insight.",
  },
  {
    path: "/automations",
    anchor: [".auto-table"],
    text: "Automations are rules that run on each new moment. This one fills in service details.",
  },
  { text: "That's the tour. Click around freely; the demo is read-only, so nothing is saved." },
]

const SEEN_KEY = "context.tour.seen"
const GAP = 12
const EDGE = 16

export function tourSeen(): boolean {
  try {
    return localStorage.getItem(SEEN_KEY) === "1"
  } catch {
    return false
  }
}

function markSeen(): void {
  try {
    localStorage.setItem(SEEN_KEY, "1")
  } catch {
    // storage blocked: the tour just offers itself again next time
  }
}

type Box = { top: number; left: number; width: number; height: number }

const sameBox = (a: Box | null, b: Box | null) =>
  a === b || (!!a && !!b && a.top === b.top && a.left === b.left && a.width === b.width && a.height === b.height)

function findAnchor(step: Step): Element | null {
  for (const selector of step.anchor ?? []) {
    const el = document.querySelector(selector)
    if (el) return el
  }
  return null
}

/** Short bubbles that walk a visitor through the demo, one page element at a time. */
export default function Tour({ onClose }: { onClose: () => void }) {
  const [index, setIndex] = useState(0)
  const [box, setBox] = useState<Box | null>(null)
  const [bubble, setBubble] = useState({ width: 320, height: 120 })
  const bubbleRef = useRef<HTMLDivElement | null>(null)
  // The step whose own page has been reached; `done` only counts after that.
  const arrived = useRef(-1)
  const location = useLocation()
  const navigate = useNavigate()
  const step = STEPS[index]!
  const last = index === STEPS.length - 1

  const close = () => {
    markSeen()
    onClose()
  }
  const next = () => (last ? close() : setIndex((i) => i + 1))

  useEffect(() => {
    let stale = false
    void Promise.resolve(typeof step.path === "function" ? step.path() : step.path).then((target) => {
      if (!stale && target && target !== location.pathname) navigate(target)
    })
    return () => {
      stale = true
    }
    // Only on arrival at a step; later navigation is the visitor's own.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [index])

  useEffect(() => {
    if (typeof step.path !== "string" || location.pathname === step.path) arrived.current = index
    else if (arrived.current === index && step.done?.(location.pathname)) setIndex((i) => i + 1)
  }, [location.pathname, step, index])

  // Track the anchor every frame: pages load data and shift after the step starts.
  useEffect(() => {
    let frame = 0
    let scrolled = false
    let prev: Box | null = null
    const tick = () => {
      const el = findAnchor(step)
      let nextBox: Box | null = null
      if (el) {
        if (!scrolled) {
          scrolled = true
          const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches
          el.scrollIntoView({ block: "center", behavior: reduce ? "auto" : "smooth" })
        }
        const r = el.getBoundingClientRect()
        nextBox = { top: r.top, left: r.left, width: r.width, height: r.height }
      }
      if (!sameBox(prev, nextBox)) {
        prev = nextBox
        setBox(nextBox)
      }
      frame = requestAnimationFrame(tick)
    }
    setBox(null)
    frame = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(frame)
  }, [step])

  useLayoutEffect(() => {
    const el = bubbleRef.current
    if (el) setBubble({ width: el.offsetWidth, height: el.offsetHeight })
  }, [index])

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") close()
    }
    document.addEventListener("keydown", onKey)
    return () => document.removeEventListener("keydown", onKey)
  })

  const vw = window.innerWidth
  const vh = window.innerHeight
  const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(v, Math.max(lo, hi)))
  let place: CSSProperties
  if (!box) {
    place = { top: (vh - bubble.height) / 2, left: (vw - bubble.width) / 2 }
  } else {
    const below = box.top + box.height + GAP
    const above = box.top - GAP - bubble.height
    // Below if it fits, else above, else tucked inside the anchor's lower edge.
    const top =
      below + bubble.height <= vh - EDGE ? below : above >= EDGE ? above : box.top + box.height - bubble.height - EDGE
    place = {
      top: clamp(top, EDGE, vh - bubble.height - EDGE),
      left: clamp(box.left, EDGE, vw - bubble.width - EDGE),
    }
  }

  return (
    <>
      {box && (
        <div
          className="tour-ring"
          style={{ top: box.top - 4, left: box.left - 4, width: box.width + 8, height: box.height + 8 }}
        />
      )}
      <div ref={bubbleRef} className="tour-bubble" style={place} role="dialog" aria-label="Tour">
        <p className="tour-text">{step.text}</p>
        <div className="tour-foot">
          <span className="tour-count">
            {index + 1} of {STEPS.length}
          </span>
          {!last && (
            <button className="btn ghost slim" onClick={close}>
              Skip
            </button>
          )}
          <button className="btn primary slim" onClick={next} autoFocus>
            {last ? "Done" : "Next"}
          </button>
        </div>
      </div>
    </>
  )
}
