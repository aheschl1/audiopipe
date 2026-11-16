import { useCallback, useEffect, useMemo, useState, type KeyboardEvent, type ReactNode } from "react"
import {
  getMoment,
  type MomentDetail,
  type MomentItem,
  type MomentLink,
  type MomentRelation,
} from "../api/moments"
import { clock, fmtLongDay, fmtSpan, shortId } from "../format"
import { Link } from "react-router"
import { isUnnamed, personHref } from "../moments"
import { categoryColor } from "../speakers"
import { artifactUrl } from "../visual"

type Open = (sessionId: string, seekMs?: number) => void

// How a linked item reads relative to the focused one: "K2 depicts U3" shows
// "depicts" on K2 when U3 is focused, and "depicted" on U3 when K2 is.
const ACTIVE: Record<MomentRelation, string> = {
  depicts: "depicts",
  mentions: "mentions",
  reads: "reads",
  same_scene: "same scene",
  unrelated: "coincidental",
}
const PASSIVE: Record<MomentRelation, string> = {
  depicts: "depicted",
  mentions: "mentioned",
  reads: "read",
  same_scene: "same scene",
  unrelated: "coincidental",
}
const ORDER: MomentRelation[] = ["depicts", "mentions", "reads", "same_scene", "unrelated"]
const GROUP: Record<MomentRelation, string> = {
  depicts: "Shows what was said",
  mentions: "Said about what was seen",
  reads: "Text read",
  same_scene: "Same scene",
  unrelated: "Coincidental",
}
const WEAK = 0.5

const isImage = (item: MomentItem) => item.kind === "keyframe" || item.kind === "photo"

function speakerName(speaker: string | null): string {
  return speaker ?? "Unknown"
}

function excerpt(text: string | null, max = 72): string {
  if (!text) return ""
  return text.length > max ? `${text.slice(0, max - 1).trimEnd()}…` : text
}

function useNarrow(query: string): boolean {
  const [match, setMatch] = useState(() => window.matchMedia(query).matches)
  useEffect(() => {
    const mq = window.matchMedia(query)
    const on = () => setMatch(mq.matches)
    mq.addEventListener("change", on)
    return () => mq.removeEventListener("change", on)
  }, [query])
  return match
}

/** Focus is one item (its neighbourhood lights up) or one link (its two ends do). */
type Focus = { item: string } | { link: MomentLink } | null

type Ctx = {
  byId: Map<string, MomentItem>
  neighbours: Map<string, MomentLink[]>
  focus: Focus
  selected: string | null
  hover: (focus: Focus) => void
  select: (id: string | null, scroll?: boolean) => void
}

function other(link: MomentLink, id: string): string {
  return link.from_artifact_id === id ? link.to_artifact_id : link.from_artifact_id
}

/** Lit state of one item under the current focus, and the relation word to show on it. */
function lit(ctx: Ctx, id: string): { dim: boolean; primary: boolean; tag: MomentLink | null; word: string | null } {
  const focus = ctx.focus
  if (!focus) return { dim: false, primary: false, tag: null, word: null }
  if ("link" in focus) {
    const on = focus.link.from_artifact_id === id || focus.link.to_artifact_id === id
    return { dim: !on, primary: false, tag: null, word: null }
  }
  if (focus.item === id) return { dim: false, primary: true, tag: null, word: null }
  const link = (ctx.neighbours.get(focus.item) ?? []).find((l) => other(l, focus.item) === id) ?? null
  if (!link) return { dim: true, primary: false, tag: null, word: null }
  const word = link.from_artifact_id === id ? ACTIVE[link.relation] : PASSIVE[link.relation]
  return { dim: false, primary: false, tag: link, word }
}

function Refs({ ctx, id }: { ctx: Ctx; id: string }) {
  // A pair can carry several relations (mentions and reads); one ref per target.
  const byTarget = new Map<string, MomentLink[]>()
  for (const l of ctx.neighbours.get(id) ?? []) {
    const target = other(l, id)
    byTarget.set(target, [...(byTarget.get(target) ?? []), l])
  }
  if (!byTarget.size) return null
  const targets = [...byTarget.entries()]
  const shown = targets.slice(0, 3)
  return (
    <span className="refs">
      {shown.map(([targetId, ls]) => {
        const target = ctx.byId.get(targetId)
        if (!target) return null
        const weak = ls.every((l) => l.confidence < WEAK)
        return (
          <button
            key={targetId}
            className={`ref ${weak ? "weak" : ""}`}
            title={ls.map((l) => `${ACTIVE[l.relation]}: ${l.why}`).join("\n")}
            onClick={(e) => {
              e.stopPropagation()
              ctx.select(targetId, true)
            }}
          >
            {target.label}
          </button>
        )
      })}
      {targets.length > shown.length && <span className="ref-more">+{targets.length - shown.length}</span>}
    </span>
  )
}

function RelationTag({ link, word }: { link: MomentLink | null; word: string | null }) {
  if (!link || !word) return null
  return <span className={`rel-tag ${link.confidence < WEAK ? "weak" : ""}`}>{word}</span>
}

/** Shared pointer/keyboard wiring for anything selectable. */
function itemProps(ctx: Ctx, id: string) {
  return {
    id: `mi-${id}`,
    role: "button",
    tabIndex: 0,
    "aria-pressed": ctx.selected === id,
    onMouseEnter: () => ctx.hover({ item: id }),
    onMouseLeave: () => ctx.hover(null),
    onClick: () => ctx.select(ctx.selected === id ? null : id),
    onKeyDown: (e: KeyboardEvent) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault()
        ctx.select(ctx.selected === id ? null : id)
      }
    },
  }
}

function Line({ ctx, item, showSpeaker, showTime }: { ctx: Ctx; item: MomentItem; showSpeaker: boolean; showTime: boolean }) {
  const state = lit(ctx, item.artifact_id)
  const cls = `line ${state.dim ? "dim" : ""} ${ctx.selected === item.artifact_id ? "selected" : ""} ${
    item.kind === "sound" ? "sound" : ""
  }`
  return (
    <div className={cls} {...itemProps(ctx, item.artifact_id)}>
      <span className="line-time">{showTime ? clock(item.start_at) : ""}</span>
      <span className="line-main">
        {item.kind === "sound" ? (
          <span className="line-sound">{item.text}</span>
        ) : (
          <>
            {showSpeaker && (
              <span className="line-speaker">
                <span className="speaker-dot" style={{ background: categoryColor(item.speaker_id ?? item.speaker ?? "?") }} />
                {speakerName(item.speaker)}
              </span>
            )}
            <span className="line-text">{item.text}</span>
          </>
        )}
      </span>
      <span className="line-aside">
        <RelationTag link={state.tag} word={state.word} />
        <Refs ctx={ctx} id={item.artifact_id} />
      </span>
    </div>
  )
}

function Tile({ ctx, item }: { ctx: Ctx; item: MomentItem }) {
  const state = lit(ctx, item.artifact_id)
  return (
    <div
      className={`tile ${state.dim ? "dim" : ""} ${ctx.selected === item.artifact_id ? "selected" : ""}`}
      {...itemProps(ctx, item.artifact_id)}
      aria-label={`${item.kind === "photo" ? "Photo" : "Video frame"} at ${clock(item.start_at)}`}
    >
      <span className="tile-img">
        <img src={artifactUrl(item.artifact_id)} alt="" loading="lazy" />
      </span>
      <span className="tile-cap">
        <span className="tile-label">{item.label}</span>
        <span className="row-dim">{clock(item.start_at)}</span>
        <span className="tile-aside">
          <RelationTag link={state.tag} word={state.word} />
          <Refs ctx={ctx} id={item.artifact_id} />
        </span>
      </span>
    </div>
  )
}

/** Items in time order: lines as rows, runs of consecutive images as a grid. */
function Stream({ ctx, items }: { ctx: Ctx; items: MomentItem[] }) {
  const out: ReactNode[] = []
  let run: MomentItem[] = []
  let prev: MomentItem | null = null
  const flush = () => {
    if (!run.length) return
    out.push(
      <div key={`g-${run[0]!.artifact_id}`} className="tiles">
        {run.map((item) => (
          <Tile key={item.artifact_id} ctx={ctx} item={item} />
        ))}
      </div>,
    )
    run = []
  }
  for (const item of items) {
    if (isImage(item)) {
      run.push(item)
      continue
    }
    flush()
    const newMinute = !prev || clock(prev.start_at) !== clock(item.start_at)
    const newSpeaker = !prev || prev.kind !== "utterance" || prev.speaker !== item.speaker
    out.push(
      <Line key={item.artifact_id} ctx={ctx} item={item} showSpeaker={newSpeaker} showTime={newMinute} />,
    )
    prev = item
  }
  flush()
  return <>{out}</>
}

/** A compact stand-in for an item inside a connection: a thumbnail or the speaker, plus its label. */
function Mention({ item }: { item: MomentItem }) {
  return (
    <span className="mention">
      {isImage(item) ? (
        <span className="mention-thumb">
          <img src={artifactUrl(item.artifact_id)} alt="" loading="lazy" />
        </span>
      ) : (
        <span className="mention-who">{item.kind === "utterance" ? speakerName(item.speaker) : item.text}</span>
      )}
      <span className="mention-label">{item.label}</span>
    </span>
  )
}

function Connection({ ctx, link }: { ctx: Ctx; link: MomentLink }) {
  const from = ctx.byId.get(link.from_artifact_id)
  const to = ctx.byId.get(link.to_artifact_id)
  if (!from || !to) return null
  const quotes = [from, to].filter((i) => i.kind === "utterance" && i.text)
  return (
    <li
      className={`conn ${link.confidence < WEAK ? "weak" : ""}`}
      onMouseEnter={() => ctx.hover({ link })}
      onMouseLeave={() => ctx.hover(null)}
    >
      <button className="conn-hit" onClick={() => ctx.select(from.artifact_id, true)}>
        <span className="conn-sentence">
          <Mention item={from} />
          <span className="conn-verb">{ACTIVE[link.relation]}</span>
          <Mention item={to} />
        </span>
        {quotes.map((q) => (
          <span key={q.artifact_id} className="conn-quote">
            “{excerpt(q.text, 96)}”
          </span>
        ))}
        {link.why && <span className="conn-why">{link.why}</span>}
        <span className="conn-conf">
          {Math.round(link.confidence * 100)}% sure{link.confidence < WEAK ? " · weak" : ""}
        </span>
      </button>
    </li>
  )
}

function Selected({ ctx, item, onOpen }: { ctx: Ctx; item: MomentItem; onOpen: Open }) {
  return (
    <div className="picked">
      <div className="picked-head">
        <span className="section-label">
          {item.label} · {item.kind === "keyframe" ? "video frame" : item.kind}
        </span>
        <button className="btn ghost slim" onClick={() => ctx.select(null)} title="Clear (Esc)">
          ✕
        </button>
      </div>
      {isImage(item) ? (
        <span className="picked-img">
          <img src={artifactUrl(item.artifact_id)} alt="" />
        </span>
      ) : (
        <p className="picked-text">
          {item.kind === "utterance" && <strong>{speakerName(item.speaker)} </strong>}
          {item.text}
        </p>
      )}
      <div className="picked-foot">
        <span className="row-dim">{clock(item.start_at)}</span>
        <button className="btn slim" onClick={() => onOpen(item.session_id, item.at_ms ?? undefined)}>
          {isImage(item) ? "Open in recording" : "Listen in recording"}
        </button>
      </div>
    </div>
  )
}

function Facts({ moment }: { moment: MomentDetail }) {
  const heard = moment.speakers
  const heardNames = new Set(heard.map((sp) => sp.name.toLowerCase()))
  const mentioned = moment.people.filter((p) => !heardNames.has(p.toLowerCase()))
  const rows: [string, ReactNode][] = []
  if (heard.length)
    rows.push([
      "People",
      heard.map((sp) => {
        const body = (
          <>
            <span className="speaker-dot" style={{ background: categoryColor(sp.speaker_id ?? sp.name) }} />
            {sp.name}
          </>
        )
        return sp.speaker_id ? (
          <Link key={sp.name} to={personHref(sp.speaker_id)} className="chip as-link" title="Open in People">
            {body}
          </Link>
        ) : (
          <span key={sp.name} className={`chip ${isUnnamed(sp) ? "unnamed" : ""}`}>
            {body}
          </span>
        )
      }),
    ])
  if (mentioned.length)
    rows.push([
      "Also mentioned",
      mentioned.map((p) => (
        <span key={p} className="chip quiet">
          {p}
        </span>
      )),
    ])
  if (moment.topics.length)
    rows.push(["Topics", moment.topics.map((t) => <span key={t} className="chip">{t}</span>)])
  if (moment.visible_text.length)
    rows.push([
      "Text seen",
      moment.visible_text.map((t) => (
        <span key={t} className="chip seen-text">
          “{t}”
        </span>
      )),
    ])
  if (!rows.length) return null
  return (
    <dl className="facts">
      {rows.map(([label, value]) => (
        <div key={label} className="fact">
          <dt className="section-label">{label}</dt>
          <dd className="chips">{value}</dd>
        </div>
      ))}
    </dl>
  )
}

/** What the user's automations filled in for this moment: one panel per matching rule,
 * set apart from what the moment tier itself wrote. */
function AutomationFacts({ moment }: { moment: MomentDetail }) {
  if (!moment.automations.length) return null
  return (
    <section className="moment-automations" aria-label="Automations that matched">
      {moment.automations.map((a) => {
        const fields = Object.entries(a.fields)
        return (
          <div key={a.automation_id} className="auto-card">
            <div className="auto-card-head">
              <span className="auto-badge">Automation</span>
              <Link className="auto-card-name" to={`/automations/${a.automation_id}`}>
                {a.name}
              </Link>
              <span className="auto-card-note">matched this moment</span>
            </div>
            {fields.length > 0 && (
              <dl className="kv">
                {fields.map(([k, v]) => (
                  <div key={k}>
                    <dt>{k}</dt>
                    <dd className={v ? "" : "row-dim"}>{v || "—"}</dd>
                  </div>
                ))}
              </dl>
            )}
          </div>
        )
      })}
    </section>
  )
}

/** The contributing recordings: an ingest detail, kept reachable but out of the way. */
function Sources({ moment, onOpen }: { moment: MomentDetail; onOpen: Open }) {
  const per = new Map<string, { first: MomentItem; lines: number; images: number }>()
  for (const item of moment.items) {
    const entry = per.get(item.session_id)
    if (!entry) per.set(item.session_id, { first: item, lines: 0, images: 0 })
    const e = per.get(item.session_id)!
    if (item.start_at < e.first.start_at) e.first = item
    if (item.kind === "utterance") e.lines++
    if (isImage(item)) e.images++
  }
  const ids = [...new Set([...moment.session_ids, ...per.keys()])]
  if (!ids.length) return null
  return (
    <details className="sources">
      <summary>
        Built from {ids.length} recording{ids.length === 1 ? "" : "s"}
      </summary>
      <div className="list">
        {ids.map((id) => {
          const e = per.get(id)
          const what = e
            ? [e.lines ? `${e.lines} lines` : null, e.images ? `${e.images} images` : null].filter(Boolean).join(" · ")
            : ""
          return (
            <button
              key={id}
              className="row"
              onClick={() => onOpen(id, e?.first.at_ms ?? undefined)}
            >
              <span className="session-time">{e ? clock(e.first.start_at) : "—"}</span>
              <span className="row-main">
                <span className="row-title mono">{shortId(id)}</span>
                {what && <span className="row-sub">{what}</span>}
              </span>
            </button>
          )
        })}
      </div>
    </details>
  )
}

/** Where to land inside a moment: an exact item, else the item nearest an instant of a recording. */
export type MomentTarget = { item?: string; sessionId?: string; ms?: number }

function findTarget(items: MomentItem[], target: MomentTarget): MomentItem | null {
  const exact = target.item ? items.find((i) => i.artifact_id === target.item) : undefined
  if (exact) return exact
  if (!target.sessionId || target.ms === undefined) return null
  let best: MomentItem | null = null
  for (const i of items) {
    if (i.session_id !== target.sessionId || i.at_ms === null || i.kind === "sound") continue
    if (!best || Math.abs(i.at_ms - target.ms) < Math.abs(best.at_ms! - target.ms)) best = i
  }
  return best
}

function typing(e: globalThis.KeyboardEvent): boolean {
  const el = e.target as HTMLElement | null
  return !!el && (el.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName))
}

export default function MomentView({
  momentId,
  target,
  onBack,
  onNeighbour,
  onOpenSession,
}: {
  momentId: string
  target?: MomentTarget
  onBack: () => void
  onNeighbour: (id: string) => void
  onOpenSession: Open
}) {
  const [moment, setMoment] = useState<MomentDetail | null>(null)
  const [status, setStatus] = useState<number | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [hovered, setHovered] = useState<Focus>(null)
  const [showUnrelated, setShowUnrelated] = useState(false)
  const narrow = useNarrow("(max-width: 699px)")

  const load = useCallback(async () => {
    setStatus(null)
    const { data, status: code } = await getMoment(momentId)
    if (data) setMoment(data)
    else setStatus(code)
  }, [momentId])
  useEffect(() => {
    void load()
  }, [load])

  const older = moment?.previous_id ?? null
  const newer = moment?.next_id ?? null
  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey || typing(e)) return
      if (e.key === "Escape") setSelected(null)
      else if ((e.key === "j" || e.key === "ArrowLeft") && older) onNeighbour(older)
      else if ((e.key === "k" || e.key === "ArrowRight") && newer) onNeighbour(newer)
    }
    document.addEventListener("keydown", onKey)
    return () => document.removeEventListener("keydown", onKey)
  }, [older, newer, onNeighbour])

  // Arriving from search: select the matching item once the moment loads.
  const targetKey = target ? `${target.item ?? ""}|${target.sessionId ?? ""}|${target.ms ?? ""}` : ""
  useEffect(() => {
    if (!moment || !target) return
    const hit = findTarget(moment.items, target)
    if (!hit) return
    setSelected(hit.artifact_id)
    requestAnimationFrame(() =>
      document.getElementById(`mi-${hit.artifact_id}`)?.scrollIntoView({ block: "center" }),
    )
  }, [moment, targetKey])

  const graph = useMemo(() => {
    const items = [...(moment?.items ?? [])].sort((a, b) => Date.parse(a.start_at) - Date.parse(b.start_at))
    const byId = new Map(items.map((i) => [i.artifact_id, i]))
    const all = (moment?.links ?? []).filter((l) => byId.has(l.from_artifact_id) && byId.has(l.to_artifact_id))
    const unrelated = all.filter((l) => l.relation === "unrelated").length
    const links = all
      .filter((l) => showUnrelated || l.relation !== "unrelated")
      .sort((a, b) => ORDER.indexOf(a.relation) - ORDER.indexOf(b.relation) || b.confidence - a.confidence)
    const neighbours = new Map<string, MomentLink[]>()
    for (const l of links) {
      for (const id of [l.from_artifact_id, l.to_artifact_id]) {
        const list = neighbours.get(id) ?? []
        list.push(l)
        neighbours.set(id, list)
      }
    }
    return { items, byId, links, neighbours, unrelated }
  }, [moment, showUnrelated])

  if (!moment)
    return status !== null ? (
      <div className="moment-view">
        <button className="crumb" onClick={onBack}>
          ← Moments
        </button>
        <div className="banner error">
          {status === 404 ? "This moment no longer exists. It may have been regrouped." : "Couldn't load this moment. "}
          {status !== 404 && (
            <button className="link-btn" onClick={() => void load()}>
              Try again
            </button>
          )}
        </div>
      </div>
    ) : (
      <div className="moment-view">
        <div className="skeleton" style={{ height: 32, width: "50%" }} />
        <div className="skeleton" style={{ height: 64 }} />
        <div className="skeleton" style={{ height: 320 }} />
      </div>
    )

  const select = (id: string | null, scroll = false) => {
    setSelected(id)
    if (id && scroll)
      document.getElementById(`mi-${id}`)?.scrollIntoView({ block: "nearest" })
  }
  const ctx: Ctx = {
    byId: graph.byId,
    neighbours: graph.neighbours,
    focus: hovered ?? (selected ? { item: selected } : null),
    selected,
    hover: setHovered,
    select,
  }
  const heard = graph.items.filter((i) => !isImage(i))
  const seen = graph.items.filter(isImage)
  const picked = selected ? graph.byId.get(selected) ?? null : null
  const shownLinks = picked ? graph.neighbours.get(picked.artifact_id) ?? [] : graph.links

  return (
    <div className={`moment-view ${picked ? "has-pick" : ""}`}>
      <header className="moment-head">
        <div className="moment-nav">
          <button className="crumb" onClick={onBack}>
            ← Moments
          </button>
          <span className="moment-steps">
            <button
              className="btn ghost slim"
              disabled={!older}
              onClick={() => older && onNeighbour(older)}
              title="Earlier moment (J or ←)"
            >
              ‹ earlier
            </button>
            <button
              className="btn ghost slim"
              disabled={!newer}
              onClick={() => newer && onNeighbour(newer)}
              title="Later moment (K or →)"
            >
              later ›
            </button>
          </span>
        </div>
        <h1 className="moment-head-title">{moment.title || "Untitled moment"}</h1>
        <div className="page-meta">
          {fmtLongDay(moment.start_at)} · {clock(moment.start_at)}–{clock(moment.end_at)} ·{" "}
          {fmtSpan(moment.start_at, moment.end_at)}
        </div>
        {moment.summary && <p className="moment-head-summary">{moment.summary}</p>}
        <Facts moment={moment} />
        <AutomationFacts moment={moment} />
      </header>

      <div className="evidence">
        {narrow ? (
          <section className="ev-col ev-stream">
            <h2 className="section-label">
              As it happened <span className="section-count">{graph.items.length}</span>
            </h2>
            <Stream ctx={ctx} items={graph.items} />
          </section>
        ) : (
          <>
            <section className="ev-col ev-heard">
              <h2 className="section-label">
                Heard <span className="section-count">{heard.length}</span>
              </h2>
              {heard.length ? <Stream ctx={ctx} items={heard} /> : <div className="empty">Nothing was said.</div>}
            </section>
            <section className="ev-col ev-seen">
              <h2 className="section-label">
                Seen <span className="section-count">{seen.length}</span>
              </h2>
              {seen.length ? <Stream ctx={ctx} items={seen} /> : <div className="empty">No photos or video.</div>}
            </section>
          </>
        )}

        <aside className="ev-aside">
          {picked && <Selected ctx={ctx} item={picked} onOpen={onOpenSession} />}
          <h2 className="section-label">
            {picked ? `${picked.label} connects to` : "Connections"}
            <span className="section-count">{shownLinks.length}</span>
          </h2>
          {!picked && shownLinks.length > 0 && (
            <p className="ev-hint">Hover or select a line or an image to see what it connects to.</p>
          )}
          {shownLinks.length === 0 ? (
            <p className="ev-hint">
              {picked ? "Nothing else in this moment is linked to it." : "No connections were drawn for this moment."}
            </p>
          ) : (
            ORDER.map((relation) => {
              const group = shownLinks.filter((l) => l.relation === relation)
              if (!group.length) return null
              return (
                <div key={relation} className="conn-group">
                  <h3 className="conn-group-label">{GROUP[relation]}</h3>
                  <ul className="conns">
                    {group.map((l) => (
                      <Connection key={`${l.from_artifact_id}-${l.to_artifact_id}-${l.relation}`} ctx={ctx} link={l} />
                    ))}
                  </ul>
                </div>
              )
            })
          )}
          {graph.unrelated > 0 && (
            <button className="btn ghost slim" onClick={() => setShowUnrelated((v) => !v)}>
              {showUnrelated ? "Hide" : "Show"} {graph.unrelated} coincidental overlap{graph.unrelated === 1 ? "" : "s"}
            </button>
          )}
        </aside>
      </div>

      <footer className="moment-foot">
        <Sources moment={moment} onOpen={onOpenSession} />
        <p className="provenance">
          Title, summary and connections written by <span className="mono">{moment.model}</span>.
        </p>
      </footer>
    </div>
  )
}
