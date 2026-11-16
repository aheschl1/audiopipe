import { useEffect, useState } from "react"

const R = 92
const CLIP_R = R - 3
const CENTERS = [120, 360, 600] as const
const CY = 118
// Each caption as its two lines.
const CAPTIONS = [
  ["You do", "the work"],
  ["Your glasses", "take the notes"],
  ["Context does", "the paperwork"],
]
const FIXED = 0

type Pt = [number, number]

// One nut, one arc, in page coordinates: constant horizontal speed, so the
// time it spends hidden between bubbles matches the distance it covers there.
// It leaves the mechanic's raised hand and comes down on the number; the apex
// shifts so the parabola passes through both at a fixed height.
const TOSS: Pt = [CENTERS[0] + 16, CY]
const TOSS_X = TOSS[0]
const LAND_Y = CY - 4
const APEX_Y = 50
const APEX_X = (() => {
  const r = Math.sqrt((TOSS[1] - APEX_Y) / (LAND_Y - APEX_Y))
  return (TOSS[0] + r * CENTERS[2]) / (1 + r)
})()
const K = (LAND_Y - APEX_Y) / (CENTERS[2] - APEX_X) ** 2
const arcY = (x: number) => APEX_Y + K * (x - APEX_X) ** 2
const SPEED = 250 // viewBox units per second
const NUT_R = 5

// Bubble one, from the cycle start: the watcher's throw comes back, the mechanic
// stands to catch it, stows it, digs in the engine, pulls it out, and tosses it.
const PITCH_MS = 700
const STOW_MS = 450
const DIG_MS = 1100
const PULL_MS = 300
const THROW_MS = 380
const T_DIG = PITCH_MS + STOW_MS
const T_PULL = T_DIG + DIG_MS
const T_THROW = T_PULL + PULL_MS
const T_TOSS = T_THROW + THROW_MS
const FLIP_MS = 320

// Off the number: a short pop up, a little drift, then down onto the swoop.
const BOUNCE_VX = 35
const BOUNCE_VY = -95
const BOUNCE_G = 280

// Then everything rests in focus: gusts roll the nut back along the logo's curve, past
// the watcher; he reaches for it and it flies back up into his hand, then he throws it back.
const ROLL_MS = 6000
const REACH_MS = 450
const FLY_MS = 650
const HOLD_MS = 250
const RETURN_MS = 400
const WINDUP_MS = 500
const WATCHER_THROW_MS = 250
const REST_MS = ROLL_MS + REACH_MS + FLY_MS + HOLD_MS + RETURN_MS + WINDUP_MS + WATCHER_THROW_MS
// Where the nut stops, a little past him.
const PASS_X = CENTERS[1] - 48
const ARM = 17
const DEG = Math.PI / 180

const clamp = (v: number) => Math.max(0, Math.min(1, v))
const ease = (v: number) => 1 - Math.pow(1 - clamp(v), 3)
const easeIO = (v: number) => {
  const c = clamp(v)
  return c < 0.5 ? 4 * c ** 3 : 1 - (-2 * c + 2) ** 3 / 2
}
const lerp = (a: Pt, b: Pt, t: number): Pt => [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]
const quad = (a: Pt, c: Pt, b: Pt, t: number): Pt => {
  const u = 1 - t
  return [u * u * a[0] + 2 * u * t * c[0] + t * t * b[0], u * u * a[1] + 2 * u * t * c[1] + t * t * b[1]]
}
const inside = (x: number, y: number, i: number) => (x - CENTERS[i]!) ** 2 + (y - CY) ** 2 < CLIP_R ** 2

/** Where along the arc the nut first enters bubble `i`. */
function entryX(i: number): number {
  let x = TOSS_X
  while (!inside(x, arcY(x), i)) x += 0.5
  return x
}

const msAt = (x: number) => T_TOSS + ((x - TOSS_X) / SPEED) * 1000
const T_SEEN = msAt(entryX(1))
const T_ARRIVES = msAt(entryX(2))
// The arc comes down on the number in the middle of bubble three.
const T_LANDS = msAt(CENTERS[2])

// The logo's swoop (60x60 mark: M0 22 C16 26 28 38 42 40 C50 41 56 39 60 37), stretched under the bubbles.
const WAVE_X = 12
const waveYOf = (ly: number) => 214 + (ly - 22) * 2
const LOGO_CURVE: Pt[][] = [
  [[0, 22], [16, 26], [28, 38], [42, 40]],
  [[42, 40], [50, 41], [56, 39], [60, 37]],
]
const WAVE = LOGO_CURVE.map((seg) => seg.map(([x, y]): Pt => [x * WAVE_X, waveYOf(y)]))
const WAVE_D = `M${WAVE[0]![0]!.join(" ")} ${WAVE.map((seg) => `C${seg.slice(1).map((p) => p.join(" ")).join(" ")}`).join(" ")}`
const WAVE_PTS: Pt[] = WAVE.flatMap((seg) =>
  Array.from({ length: 200 }, (_, i): Pt => {
    const t = i / 199
    const u = 1 - t
    const [a, b, c, d] = seg as [Pt, Pt, Pt, Pt]
    return [
      u ** 3 * a[0] + 3 * u * u * t * b[0] + 3 * u * t * t * c[0] + t ** 3 * d[0],
      u ** 3 * a[1] + 3 * u * u * t * b[1] + 3 * u * t * t * c[1] + t ** 3 * d[1],
    ]
  }),
).sort((p, q) => p[0] - q[0])

/** Where the nut's centre sits when resting on the swoop at `x`. */
function restY(x: number): number {
  const i = Math.max(1, WAVE_PTS.findIndex((p) => p[0] >= x))
  const [x0, y0] = WAVE_PTS[i - 1]!
  const [x1, y1] = WAVE_PTS[i] ?? WAVE_PTS[i - 1]!
  const y = x1 === x0 ? y0 : y0 + ((y1 - y0) * (x - x0)) / (x1 - x0)
  return y - NUT_R - 1.5
}

const bounceAt = (s: number): Pt => [CENTERS[2] + BOUNCE_VX * s, LAND_Y + BOUNCE_VY * s + 0.5 * BOUNCE_G * s * s]
// Time for the bounce to come down on the swoop.
const FALL_MS = (() => {
  let s = 0.05
  while (bounceAt(s)[1] < restY(bounceAt(s)[0])) s += 0.002
  return s * 1000
})()
const T_REST = T_LANDS + FALL_MS
const ROLL_FROM = bounceAt(FALL_MS / 1000)[0]
const CYCLE_MS = T_REST + REST_MS
const STILL_MS = T_REST

// Watcher keyframes within the rest, as offsets from T_REST.
const R_REACH = ROLL_MS
const R_FLY = R_REACH + REACH_MS
const R_HOLD = R_FLY + FLY_MS
const R_RETURN = R_HOLD + HOLD_MS
const R_WINDUP = R_RETURN + RETURN_MS
const R_THROW = R_WINDUP + WINDUP_MS

// Gusty, uneven progress for the roll back: overlapping gusts on a slow drift,
// easing in and out so it starts and stops at rest.
const ROLL_STEPS = 400
const gust = (u: number) =>
  0.15 +
  1.4 * Math.max(0, Math.sin(2 * Math.PI * (1.7 * u + 0.15))) ** 3 +
  0.9 * Math.max(0, Math.sin(2 * Math.PI * (3.1 * u + 0.6))) ** 4 +
  0.5 * Math.max(0, Math.sin(2 * Math.PI * (5.3 * u + 0.35))) ** 2
const ROLL: number[] = (() => {
  const acc = [0]
  for (let i = 1; i <= ROLL_STEPS; i++) {
    const u = i / ROLL_STEPS
    acc.push(acc[i - 1]! + gust(u) * Math.sqrt(Math.sin(Math.PI * u)))
  }
  return acc.map((v) => v / acc[ROLL_STEPS]!)
})()
const rollAt = (u: number) => {
  const f = clamp(u) * ROLL_STEPS
  const i = Math.min(ROLL_STEPS - 1, Math.floor(f))
  return ROLL[i]! + (ROLL[i + 1]! - ROLL[i]!) * (f - i)
}

/* ---------- the mechanic (bubble one, local coordinates) ---------- */

type Mech = { lean: number; hand: Pt; holding: boolean }

// Standing upright at the car's nose, reaching into the bay; same build as the watcher.
const HIP: Pt = [0, 34]
const DIG: Pt = [-18, 21]
const PULLED: Pt = [-14, 8]
// An underhand toss: the hand dips in front of him, then lifts and lets go.
const OVERHEAD: Pt = [6, 34]
const RELEASE_L: Pt = [TOSS[0] - CENTERS[0], TOSS[1] - CY]
const CATCH_L: Pt = [14, 4]
const CATCH: Pt = [CENTERS[0] + CATCH_L[0], CY + CATCH_L[1]]
// Waiting for the nut to come back: upright, hand at his side.
const IDLE_LEAN = 1
const IDLE_HAND: Pt = [10, 32]

const digging = (ms: number): Pt => [DIG[0] + 2.5 * Math.sin(ms / 55), DIG[1] + 1.8 * Math.cos(ms / 40)]

/** Lean (0 bent into the engine, 1 upright), working hand, and whether it holds the nut. */
function mechAt(ms: number): Mech {
  if (ms < PITCH_MS) {
    // Raises a hand to catch the throw.
    const t = easeIO(ms / (PITCH_MS - 150))
    return { lean: IDLE_LEAN + (0.9 - IDLE_LEAN) * t, hand: lerp(IDLE_HAND, CATCH_L, t), holding: false }
  }
  if (ms < T_DIG) {
    const t = easeIO((ms - PITCH_MS) / STOW_MS)
    return { lean: 0.9 * (1 - t), hand: lerp(CATCH_L, digging(ms), t), holding: t < 0.85 }
  }
  if (ms < T_PULL) return { lean: 0, hand: digging(ms), holding: false }
  if (ms < T_THROW) {
    const t = easeIO((ms - T_PULL) / PULL_MS)
    return { lean: 0.3 * t, hand: lerp(digging(ms), PULLED, t), holding: true }
  }
  if (ms < T_TOSS) {
    const t = (ms - T_THROW) / THROW_MS
    return { lean: 0.3 + 0.5 * easeIO(t), hand: quad(PULLED, OVERHEAD, RELEASE_L, t * t), holding: true }
  }
  // Follow-through, then he stops and waits for the nut to come back.
  const r = ms - T_TOSS
  if (r < 300) return { lean: 0.8, hand: lerp(RELEASE_L, [18, 2], ease(r / 300)), holding: false }
  const t = easeIO((r - 300) / 600)
  return { lean: 0.8 + (IDLE_LEAN - 0.8) * t, hand: lerp([18, 2], IDLE_HAND, t), holding: false }
}

/* ---------- the watcher (bubble two, local coordinates) ---------- */

const SHOULDER: Pt = [0, 18]
const reach = (angle: number, len = ARM): Pt => [len * Math.cos(angle * DEG), SHOULDER[1] + len * Math.sin(angle * DEG)]
const PASSED: Pt = [PASS_X, restY(PASS_X)]
// Reaching down and back toward where the nut stopped, arm at full stretch.
const REACH_DEG = Math.atan2(PASSED[1] - (CY + SHOULDER[1]), PASSED[0] - CENTERS[1]) / DEG
const REACH_HAND = reach(REACH_DEG, ARM + 4)
const WATCHER_RELEASE: Pt = [CENTERS[1] + ARM * Math.cos(-160 * DEG), CY + SHOULDER[1] + ARM * Math.sin(-160 * DEG)]

/** The watcher's throwing hand (relative to him) and whether it holds the nut. He never leaves his bubble. */
function watcherAt(ms: number): { hand: Pt; holding: boolean } {
  const side = reach(45)
  if (ms < T_REST) {
    // Follow-through after his throw, then the arm drops back to his side.
    const a = ms < 500 ? -160 - 65 * ease(ms / 500) : ms < 1100 ? -225 - 90 * easeIO((ms - 500) / 600) : 45
    return { hand: reach(a), holding: false }
  }
  const r = ms - T_REST
  if (r < R_REACH) return { hand: side, holding: false }
  if (r < R_FLY) return { hand: lerp(side, REACH_HAND, easeIO((r - R_REACH) / REACH_MS)), holding: false }
  if (r < R_HOLD) return { hand: REACH_HAND, holding: false }
  if (r < R_RETURN) return { hand: REACH_HAND, holding: true }
  if (r < R_WINDUP) return { hand: lerp(REACH_HAND, side, easeIO((r - R_RETURN) / RETURN_MS)), holding: true }
  if (r < R_THROW) return { hand: reach(45 - 105 * easeIO((r - R_WINDUP) / WINDUP_MS)), holding: true }
  return { hand: reach(-60 - 100 * ((r - R_THROW) / WATCHER_THROW_MS) ** 2), holding: true }
}

/** The nut lifting off the curve into his hand: it first carries on a touch, then swings back up to him. */
function flyAt(t: number): Pt {
  const end: Pt = [CENTERS[1] + REACH_HAND[0], CY + REACH_HAND[1]]
  return quad(PASSED, [PASSED[0] - 22, Math.min(PASSED[1], end[1]) - 30], end, t)
}

/** The nut at time `ms`, or null while it's down in the engine. */
function nutAt(ms: number): Pt | null {
  if (ms < PITCH_MS) {
    const [x, y] = lerp(WATCHER_RELEASE, CATCH, ms / PITCH_MS)
    return [x, y - 22 * Math.sin(Math.PI * (ms / PITCH_MS))]
  }
  if (ms < T_TOSS) {
    const m = mechAt(ms)
    return m.holding ? [CENTERS[0] + m.hand[0], CY + m.hand[1]] : null
  }
  if (ms < T_LANDS) {
    const x = TOSS_X + (SPEED * (ms - T_TOSS)) / 1000
    return [x, arcY(x)]
  }
  if (ms < T_REST) return bounceAt((ms - T_LANDS) / 1000)
  const r = ms - T_REST
  const w = watcherAt(ms)
  if (w.holding) return [CENTERS[1] + w.hand[0], CY + w.hand[1]]
  if (r >= R_FLY) return flyAt(easeIO((r - R_FLY) / FLY_MS))
  const x = ROLL_FROM - (ROLL_FROM - PASS_X) * rollAt(r / ROLL_MS)
  return [x, restY(x)]
}

/** How hard the wind is pushing right now, 0 to 1, for the gust streaks. */
function windAt(ms: number): number {
  if (ms < T_REST || ms > T_REST + ROLL_MS) return 0
  const u = (ms - T_REST) / ROLL_MS
  return clamp((gust(u) - 0.6) / 1.4) * Math.sqrt(Math.sin(Math.PI * u))
}

function useClock(still: boolean): number {
  const [now, setNow] = useState(0)
  useEffect(() => {
    if (still) return
    let frame = 0
    const start = performance.now()
    const tick = (t: number) => {
      setNow(t - start)
      frame = requestAnimationFrame(tick)
    }
    frame = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(frame)
  }, [still])
  return now
}

function useReducedMotion(): boolean {
  const [reduce, setReduce] = useState(() => window.matchMedia("(prefers-reduced-motion: reduce)").matches)
  useEffect(() => {
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)")
    const on = () => setReduce(mq.matches)
    mq.addEventListener("change", on)
    return () => mq.removeEventListener("change", on)
  }, [])
  return reduce
}

const HEX = Array.from({ length: 6 }, (_, i) => {
  const a = (60 * i + 30) * DEG
  return `${(NUT_R * Math.cos(a)).toFixed(2)},${(NUT_R * Math.sin(a)).toFixed(2)}`
}).join(" ")

/** Bubble one: a mechanic bent over an open engine bay. */
function Mechanic({ ms }: { ms: number }) {
  const { lean, hand } = mechAt(ms)
  // The other hand rests on the fender while he works, at his side otherwise.
  const brace = lerp([-12, 28], [-8, 31], lean)
  return (
    <g transform={`translate(${CENTERS[0]} ${CY})`}>
      <line className="hs-ground" x1={-80} y1={58} x2={80} y2={58} />
      {/* the car's nose in profile: hood at hip height, propped open over the engine */}
      <path className="hs-car" d="M -62 28 L -32 -14" />
      <line className="hs-car-thin" x1={-14} y1={28} x2={-40} y2={-3} />
      <rect className="hs-engine" x={-46} y={20} width={30} height={8} rx={2} />
      <circle className="hs-engine" cx={-31} cy={17} r={3} />
      <path
        className="hs-car-body"
        d="M -95 48 H -53 A 9 9 0 0 1 -35 48 H -12 Q -8 48 -8 44 V 32 Q -8 28 -12 28 H -62 L -78 8 H -95 Z"
      />
      <rect className="hs-car-thin" x={-13} y={31} width={4} height={4} rx={1} />
      <circle className="hs-car-body" cx={-44} cy={49.5} r={8.5} />
      <circle className="hs-car-thin" cx={-44} cy={49.5} r={3} />
      {/* the mechanic, upright like the watcher */}
      <circle className="hs-ink" cx={0} cy={-2} r={12} />
      <line className="hs-ink" x1={0} y1={10} x2={0} y2={HIP[1]} />
      <line className="hs-ink" x1={0} y1={18} x2={brace[0]} y2={brace[1]} />
      <line className="hs-ink" x1={0} y1={18} x2={hand[0]} y2={hand[1]} />
      <line className="hs-ink" x1={0} y1={HIP[1]} x2={-6} y2={57} />
      <line className="hs-ink" x1={0} y1={HIP[1]} x2={8} y2={57} />
    </g>
  )
}

/** Bubble two: someone in glasses, eyes on the nut; later he reaches and calls it back to his hand. */
function Watcher({ ms, nut, live }: { ms: number; nut: Pt | null; live: boolean }) {
  const cx = CENTERS[1]
  const { hand } = watcherAt(ms)
  const target = nut ?? [CENTERS[0], CY]
  const dx = target[0] - cx
  const dy = target[1] - CY
  const look = Math.max(-3, Math.min(3, dx / 30))
  const lookUp = Math.max(-2.5, Math.min(2.5, dy / 30))
  const seen = live && !!nut && inside(target[0], target[1], 1)
  const caught = seen && Math.abs(dx) < 55
  const rec = Math.floor(performance.now() / 250) % 2 === 0
  return (
    <g transform={`translate(${cx} ${CY})`}>
      <circle className="hs-ink" cx={0} cy={-2} r={12} />
      <g transform={`translate(${look} ${lookUp})`}>
        <circle className="hs-glass" cx={-5} cy={-4} r={4} />
        <circle className="hs-glass" cx={5} cy={-4} r={4} />
        <line className="hs-glass" x1={-1} y1={-4} x2={1} y2={-4} />
        {live && rec && <circle className="hs-rec" cx={10} cy={-9} r={1.6} />}
      </g>
      <line className="hs-ink" x1={0} y1={10} x2={0} y2={34} />
      <line className="hs-ink" x1={0} y1={18} x2={-12} y2={30} />
      <line className="hs-ink" x1={0} y1={18} x2={hand[0]} y2={hand[1]} />
      <line className="hs-ink" x1={0} y1={34} x2={-10} y2={57} />
      <line className="hs-ink" x1={0} y1={34} x2={10} y2={57} />
      {seen && <line className="hs-sight" x1={look} y1={-4 + lookUp} x2={dx} y2={dy} />}
      {caught && (
        <g className="hs-frame" transform={`translate(${dx} ${dy})`}>
          <path d="M -11 -5 V -11 H -5" />
          <path d="M 5 -11 H 11 V -5" />
          <path d="M 11 5 V 11 H 5" />
          <path d="M -5 11 H -11 V 5" />
        </g>
      )}
    </g>
  )
}

/** Bubble three: the counter; the nut lands on it and it flips over to one more. */
function Counter({ ms, still }: { ms: number; still: boolean }) {
  const p = still ? 1 : (ms - T_LANDS) / FLIP_MS
  // As the next throw leaves, the last count quietly folds back to zero.
  const reset = still ? 1 : ms / FLIP_MS
  const flipped = p >= 0.5 || reset < 0.5
  // Fold the old number shut, then open the new one.
  const fold =
    reset < 1 ? Math.abs(Math.cos(Math.PI * reset)) : p <= 0 || p >= 1 ? 1 : Math.abs(Math.cos(Math.PI * p))
  const fresh = !still && p >= 0.5 && ms < T_LANDS + FLIP_MS + 700
  return (
    <g transform={`translate(${CENTERS[2]} ${CY})`}>
      <g transform={`translate(0 -1) scale(1 ${Math.max(0.02, fold)}) translate(0 1)`}>
        <text className={`hs-counter ${fresh ? "landed" : ""}`} x={0} y={8} textAnchor="middle">
          {flipped ? FIXED + 1 : FIXED}
        </text>
      </g>
      <text className="hs-counter-sub" x={0} y={36} textAnchor="middle">
        engines fixed
      </text>
    </g>
  )
}

/** The landing page's story: a mechanic tosses a nut, the glasses catch it, and the count goes up. */
export default function HomeStory() {
  const still = useReducedMotion()
  const now = useClock(still)
  const ms = still ? STILL_MS : now % CYCLE_MS
  const nut = still ? null : nutAt(ms)
  const resting = still || ms >= T_REST
  const phase = resting ? 3 : ms < T_SEEN ? 0 : ms < T_ARRIVES ? 1 : 2
  const wind = still ? 0 : windAt(ms)
  // Tumbling in the air; on the swoop it turns with the distance rolled.
  const spin = !nut
    ? 0
    : ms >= T_REST
      ? ((ROLL_FROM - Math.max(nut[0], PASS_X)) / (2 * Math.PI * NUT_R)) * -360
      : ms >= T_TOSS
        ? (ms - T_TOSS) * 0.9
        : 0
  // Hidden in the gaps only on the toss; the throw back and the bounce break out.
  const clipped = ms >= T_TOSS && ms < T_LANDS

  return (
    <figure className="home-story-wrap">
      <svg
        className="home-story"
        viewBox="0 0 720 260"
        role="img"
        aria-label="A mechanic working on an engine tosses a nut, someone wearing smart glasses sees it, and an engines-fixed counter goes up."
      >
        <defs>
          {CENTERS.map((cx, i) => (
            <clipPath key={i} id={`hs-clip-${i}`}>
              <circle cx={cx} cy={CY} r={CLIP_R} />
            </clipPath>
          ))}
          <clipPath id="hs-clip-flight">
            {CENTERS.map((cx) => (
              <circle key={cx} cx={cx} cy={CY} r={CLIP_R} />
            ))}
          </clipPath>
        </defs>

        <path className="hs-wave" d={WAVE_D} />

        {[0, 1].map((i) => (
          <line
            key={i}
            className={`hs-link ${phase > i ? "lit" : ""}`}
            x1={CENTERS[i]! + R}
            y1={CY}
            x2={CENTERS[i + 1]! - R}
            y2={CY}
          />
        ))}

        {CENTERS.map((cx, i) => (
          <g key={i} className={`hs-bubble ${resting || phase === i ? "active" : ""}`}>
            <circle className="hs-ring" cx={cx} cy={CY} r={R} />
            <g clipPath={`url(#hs-clip-${i})`}>
              {i === 0 && <Mechanic ms={ms} />}
              {i === 1 && <line className="hs-ground" x1={cx - 80} y1={CY + 58} x2={cx + 80} y2={CY + 58} />}
              {i === 2 && <Counter ms={ms} still={still} />}
            </g>
            {i === 1 && (
              <g>
                <Watcher ms={ms} nut={nut} live={phase === 1} />
              </g>
            )}
          </g>
        ))}

        {nut && (
          <g clipPath={clipped ? "url(#hs-clip-flight)" : undefined}>
            {wind > 0.05 && (
              <g className="hs-wind" style={{ opacity: wind }}>
                <line x1={nut[0] + 12} y1={nut[1] - 6} x2={nut[0] + 30} y2={nut[1] - 6} />
                <line x1={nut[0] + 18} y1={nut[1] + 1} x2={nut[0] + 40} y2={nut[1] + 1} />
              </g>
            )}
            <g transform={`translate(${nut[0]} ${nut[1]}) rotate(${spin})`}>
              <polygon className="hs-nut" points={HEX} />
              <circle className="hs-nut-hole" cx={0} cy={0} r={2} />
            </g>
          </g>
        )}
      </svg>
      <ol className="hs-captions">
        {CAPTIONS.map(([a, b], i) => (
          <li key={a} className={resting || phase === i ? "active" : ""}>
            {a}
            <br />
            {b}
          </li>
        ))}
      </ol>
    </figure>
  )
}
