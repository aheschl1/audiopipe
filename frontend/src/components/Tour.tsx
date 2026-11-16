import { type CSSProperties, useEffect, useLayoutEffect, useRef, useState } from "react"
import { useLocation, useNavigate } from "react-router"
import { api } from "../api/client"

// A CSS selector, or the first match whose text contains `text`.
type Anchor = string | { selector: string; text: string }

type Step = {
  // Where the step lives; a function runs on arrival, so it can read the page it leaves.
  path?: string | (() => string | null | undefined | Promise<string | null | undefined>)
  // First anchor that matches is ringed; none (or no match) centres the bubble.
  anchor?: Anchor[]
  // Clicked once on arrival, e.g. to select a line so its connections show.
  select?: Anchor
  text: string
  // The visitor got there on their own: move on.
  done?: (path: string) => boolean
}

const CV_BOOT = { selector: ".line", text: "torn, grease" }

const STEPS: Step[] = [
  {
    path: "/",
    anchor: ["a.moment"],
    text: "A mechanic wore smart glasses through one customer visit. Context (this platform) automatically built this moment. Let\u2019s look at the details.",
    done: (path) => path.startsWith("/moments/"),
  },
  {
    path: () => document.querySelector("a.moment")?.getAttribute("href"),
    anchor: [".moment-head-summary", ".moment-head-title"],
    text: "This summary is written from what the glasses heard and saw. Nobody typed anything.",
  },
  {
    anchor: [".ev-aside"],
    select: CV_BOOT,
    text: "Things that were said are linked to their proof. Here, \u201cCV boot is torn\u201d is tied to the frame where you can see the tear.",
  },
  {
    anchor: [".moment-automations"],
    select: ".fired-pill",
    text: "The Work order automation filled in the complaint, the finding and the $340 quote. This could call external software.",
  },
  {
    path: "/search?q=CV%20boot",
    anchor: [".search-bar"],
    text: "Search for what\u2019s been said or seen, across your entire library.",
  },
  {
    path: "/speakers",
    anchor: [{ selector: ".speaker-row", text: "Michelle" }, ".speaker-row", ".list"],
    text: "Voices are recognized across visits. Every conversation with Michelle is here.",
  },
  {
    path: async () => {
      const { data } = await api.GET("/v1/sessions", { params: { query: { limit: 1, offset: 0 } } })
      const id = data?.items[0]?.id
      return id ? `/sessions/${id}` : "/sessions"
    },
    anchor: [".timeline-wrap", ".session-main"],
    text: "Every moment links back to the full recording, second by second.",
  },
  {
    path: "/automations",
    anchor: [".auto-table"],
    text: "Automations kick off structured actions when anything you specify happens. They could call webhooks, other software.",
  },
  { text: "Replay the tour anytime from the menu. Feedback: ajheschl@gmail.com" },
]

const SEEN_KEY = "context.tour.seen"
const EMAIL = /([\w.+-]+@[\w-]+\.[\w.]+\w)/
const GAP = 12
const EDGE = 16
const PAD = 4
const RADIUS = 6

/** Full-viewport clip path with a rounded hole over the ring, so the anchor stays sharp. */
function holePath(box: Box, vw: number, vh: number) {
  const x = box.left - PAD
  const y = box.top - PAD
  const w = box.width + 2 * PAD
  const h = box.height + 2 * PAD
  const r = Math.min(RADIUS, w / 2, h / 2)
  const hole =
    `M${x + r},${y} H${x + w - r} A${r},${r} 0 0 1 ${x + w},${y + r} V${y + h - r} ` +
    `A${r},${r} 0 0 1 ${x + w - r},${y + h} H${x + r} A${r},${r} 0 0 1 ${x},${y + h - r} ` +
    `V${y + r} A${r},${r} 0 0 1 ${x + r},${y} Z`
  return `path(evenodd, "M0,0 H${vw} V${vh} H0 Z ${hole}")`
}

export function tourSeen(): boolean {
  try {
    return localStorage.getItem(SEEN_KEY) === "1"
  } catch {
    return false
  }
}

/** Forget the tour was seen, so the next demo sign-in plays it. */
export function resetTour(): void {
  try {
    localStorage.removeItem(SEEN_KEY)
  } catch {
    // storage blocked: the tour plays anyway
  }
}

export function markSeen(): void {
  try {
    localStorage.setItem(SEEN_KEY, "1")
  } catch {
    // storage blocked: the tour just offers itself again next time
  }
}

type Box = { top: number; left: number; width: number; height: number }

const sameBox = (a: Box | null, b: Box | null) =>
  a === b || (!!a && !!b && a.top === b.top && a.left === b.left && a.width === b.width && a.height === b.height)

function find(anchor: Anchor): Element | null {
  if (typeof anchor === "string") return document.querySelector(anchor)
  return [...document.querySelectorAll(anchor.selector)].find((el) => el.textContent?.includes(anchor.text)) ?? null
}

function findAnchor(step: Step): Element | null {
  for (const anchor of step.anchor ?? []) {
    const el = find(anchor)
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
    let selected = !step.select
    let prev: Box | null = null
    const tick = () => {
      if (!selected && step.select) {
        const target = find(step.select)
        if (target) {
          selected = true
          const on = target.getAttribute("aria-pressed") === "true" || target.getAttribute("aria-expanded") === "true"
          if (!on) (target as HTMLElement).click()
        }
      }
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
      <div className="tour-backdrop" style={box ? { clipPath: holePath(box, vw, vh) } : undefined} />
      {box && (
        <div
          className="tour-ring"
          style={{ top: box.top - PAD, left: box.left - PAD, width: box.width + 2 * PAD, height: box.height + 2 * PAD }}
        />
      )}
      <div ref={bubbleRef} className="tour-bubble" style={place} role="dialog" aria-label="Tour">
        <p className="tour-text">
          {step.text.split(EMAIL).map((part, i) =>
            i % 2 ? (
              <a key={i} href={`mailto:${part}`}>
                {part}
              </a>
            ) : (
              part
            ),
          )}
        </p>
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
