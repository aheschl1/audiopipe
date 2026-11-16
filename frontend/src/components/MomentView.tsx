import { useCallback, useEffect, useMemo, useState, type KeyboardEvent, type ReactNode } from "react"
import {
  getMoment,
  type MomentDetail,
  type MomentItem,
  type MomentLink,
  type MomentRelation,
} from "../api/moments"
import { clock, fmtLongDay, fmtSpan } from "../format"
import { Link } from "react-router"
import { isUnnamed, personHref } from "../moments"
import { categoryColor } from "../speakers"
import { artifactUrl } from "../visual"
import Menu from "./Menu"

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

const KIND: Record<MomentItem["kind"], string> = {
  utterance: "Line",
  sound: "Sound",
  keyframe: "Video frame",
  photo: "Photo",
}

/** The item itself, short: a line's speaker and opening words, or a sound's name. */
function gist(item: MomentItem, max = 28): string {
  if (item.kind === "utterance") return `${speakerName(item.speaker)}: ${excerpt(item.text, max)}`
  return excerpt(item.text, max) || KIND[item.kind]
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

type Ctx = {
  byId: Map<string, MomentItem>
  neighbours: Map<string, MomentLink[]>
  selected: string | null
  select: (id: string | null, scroll?: boolean) => void
}

function other(link: MomentLink, id: string): string {
  return link.from_artifact_id === id ? link.to_artifact_id : link.from_artifact_id
}

/** Lit state of one item under the current selection, and the relation word to show on it. */
function lit(ctx: Ctx, id: string): { dim: boolean; primary: boolean; tag: MomentLink | null; word: string | null } {
  const focus = ctx.selected
  if (!focus) return { dim: false, primary: false, tag: null, word: null }
  if (focus === id) return { dim: false, primary: true, tag: null, word: null }
  const link = (ctx.neighbours.get(focus) ?? []).find((l) => other(l, focus) === id) ?? null
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
            className={`ref ${weak ? "weak" : ""} ${isImage(target) ? "ref-img" : ""}`}
            title={ls.map((l) => `${ACTIVE[l.relation]}: ${l.why}`).join("\n")}
            aria-label={isImage(target) ? `${KIND[target.kind]} at ${clock(target.start_at)}` : gist(target, 72)}
            onClick={(e) => {
              e.stopPropagation()
              ctx.select(targetId, true)
            }}
          >
            {isImage(target) ? <img src={artifactUrl(target.artifact_id)} alt="" loading="lazy" /> : gist(target, 20)}
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

/** A compact stand-in for an item inside a connection: a thumbnail, or the speaker and their words. */
function Mention({ item }: { item: MomentItem }) {
  if (isImage(item))
    return (
      <span className="mention" title={`${KIND[item.kind]} at ${clock(item.start_at)}`}>
        <span className="mention-thumb">
          <img src={artifactUrl(item.artifact_id)} alt="" loading="lazy" />
        </span>
      </span>
    )
  return (
    <span className="mention" title={item.text ?? undefined}>
      {item.kind === "utterance" ? (
        <>
          <span className="mention-who">{speakerName(item.speaker)}</span>
          <span className="mention-text">{excerpt(item.text, 40)}</span>
        </>
      ) : (
        <span className="mention-who">{item.text}</span>
      )}
    </span>
  )
}

function Connection({ ctx, link, detail }: { ctx: Ctx; link: MomentLink; detail: boolean }) {
  const from = ctx.byId.get(link.from_artifact_id)
  const to = ctx.byId.get(link.to_artifact_id)
  if (!from || !to) return null
  const sure = `${Math.round(link.confidence * 100)}% sure`
  return (
    <li className={`conn ${link.confidence < WEAK ? "weak" : ""}`}>
      <button
        className="conn-hit"
        title={link.why ? `${link.why} (${sure})` : sure}
        onClick={() => ctx.select(from.artifact_id, true)}
      >
        <span className="conn-sentence">
          <Mention item={from} />
          <span className="conn-verb">{ACTIVE[link.relation]}</span>
          <Mention item={to} />
        </span>
        {detail && link.why && <span className="conn-why">{link.why}</span>}
      </button>
    </li>
  )
}

function Selected({ ctx, item, onOpen }: { ctx: Ctx; item: MomentItem; onOpen: Open }) {
  return (
    <div className="picked">
      <div className="picked-head">
        <span className="section-label">{KIND[item.kind]}</span>
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
  if (heard.length || mentioned.length)
    rows.push([
      "People",
      [
        ...heard.map((sp) => {
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
        ...mentioned.map((p) => (
          <span key={`m-${p}`} className="chip quiet" title="Mentioned">
            {p}
          </span>
        )),
      ],
    ])
  if (moment.topics.length)
    rows.push(["Topics", moment.topics.map((t) => <span key={t} className="chip">{t}</span>)])
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

/** Text read off the frames, tucked away: OCR is noisy, so drop strings with no letters. */
function TextSeen({ moment }: { moment: MomentDetail }) {
  const text = moment.visible_text.filter((t) => /\p{L}/u.test(t))
  if (!text.length) return null
  return (
    <details className="text-seen">
      <summary className="section-label">
        Text seen <span className="section-count">{text.length}</span>
      </summary>
      <div className="chips">
        {text.map((t) => (
          <span key={t} className="chip seen-text">
            “{t}”
          </span>
        ))}
      </div>
    </details>
  )
}

/** What the user's automations filled in for this moment: one panel per matching rule,
 * set apart from what the moment tier itself wrote. */
/** Each automation that fired on this moment, as a full-width pill that expands to what it pulled out. */
function AutomationFacts({ moment }: { moment: MomentDetail }) {
  if (!moment.automations.length) return null
  return (
    <section className="moment-automations" aria-label="Automations that fired">
      {moment.automations.map((a) => (
        <FiredPill key={a.automation_id} automation={a} />
      ))}
    </section>
  )
}

function FiredPill({ automation: a }: { automation: MomentDetail["automations"][number] }) {
  const [open, setOpen] = useState(false)
  const fields = Object.entries(a.fields).filter(([, v]) => v)
  return (
    <div className={`fired ${open ? "open" : ""}`}>
      <button type="button" className="fired-pill" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        <span className="fired-dot" aria-hidden="true" />
        <span className="fired-verb">Fired</span>
        <span className="fired-name">{a.name}</span>
        {fields.length > 0 && (
          <span className="fired-count">
            {fields.length} field{fields.length === 1 ? "" : "s"}
          </span>
        )}
        <span className="fired-chevron" aria-hidden="true">
          {open ? "▴" : "▾"}
        </span>
      </button>
      {open && (
        <div className="fired-body">
          {fields.length > 0 && (
            <dl className="fired-fields">
              {fields.map(([k, v]) => (
                <div key={k}>
                  <dt>{k}</dt>
                  <dd>{v}</dd>
                </div>
              ))}
            </dl>
          )}
          <Link className="fired-link" to={`/automations/${a.automation_id}`}>
            View automation ›
          </Link>
        </div>
      )}
    </div>
  )
}

type Recording = { id: string; first: MomentItem | null; last: MomentItem | null }

/** The recordings a moment draws on, with its earliest and latest item in each. */
function recordings(moment: MomentDetail): Recording[] {
  const per = new Map<string, Recording>()
  for (const id of moment.session_ids) per.set(id, { id, first: null, last: null })
  for (const item of moment.items) {
    const e = per.get(item.session_id) ?? { id: item.session_id, first: null, last: null }
    if (!e.first || item.start_at < e.first.start_at) e.first = item
    if (!e.last || item.start_at > e.last.start_at) e.last = item
    per.set(item.session_id, e)
  }
  return [...per.values()].sort((a, b) => (a.first?.start_at ?? "").localeCompare(b.first?.start_at ?? ""))
}

const recordingLabel = (r: Recording) =>
  r.first && r.last
    ? `${clock(r.first.start_at)} · ${fmtSpan(r.first.start_at, r.last.start_at)}`
    : "Recording"

/** The way out to the raw recording: a link for one, a short menu for several. */
function FullRecording({ list, onOpen }: { list: Recording[]; onOpen: Open }) {
  const open = (r: Recording) => onOpen(r.id, r.first?.at_ms ?? undefined)
  if (!list.length) return null
  if (list.length === 1)
    return (
      <button className="link-btn moment-full-recording" onClick={() => open(list[0]!)}>
        Full recording ›
      </button>
    )
  return (
    <span className="moment-full-recording">
      <Menu
        label="Full recording"
        trigger="Full recording ›"
        items={list.map((r) => ({ label: recordingLabel(r), onSelect: () => open(r) }))}
      />
    </span>
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
    const links = (moment?.links ?? [])
      .filter((l) => l.relation !== "unrelated" && byId.has(l.from_artifact_id) && byId.has(l.to_artifact_id))
      .sort((a, b) => ORDER.indexOf(a.relation) - ORDER.indexOf(b.relation) || b.confidence - a.confidence)
    const neighbours = new Map<string, MomentLink[]>()
    for (const l of links) {
      for (const id of [l.from_artifact_id, l.to_artifact_id]) {
        const list = neighbours.get(id) ?? []
        list.push(l)
        neighbours.set(id, list)
      }
    }
    return { items, byId, links, neighbours }
  }, [moment])

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
    selected,
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
              aria-label="Earlier moment"
            >
              ‹
            </button>
            <button
              className="btn ghost slim"
              disabled={!newer}
              onClick={() => newer && onNeighbour(newer)}
              title="Later moment (K or →)"
              aria-label="Later moment"
            >
              ›
            </button>
          </span>
        </div>
        <h1 className="moment-head-title">{moment.title || "Untitled moment"}</h1>
        <div className="page-meta">
          {fmtLongDay(moment.start_at)} · {clock(moment.start_at)} · {fmtSpan(moment.start_at, moment.end_at)}
          <FullRecording list={recordings(moment)} onOpen={onOpenSession} />
        </div>
        {moment.summary && (
          <p className="moment-head-summary" title={`Written by ${moment.model}`}>
            {moment.summary}
          </p>
        )}
        <AutomationFacts moment={moment} />
        <Facts moment={moment} />
      </header>

      <div className="evidence">
        {narrow ? (
          <section className="ev-col ev-stream">
            <h2 className="section-label">As it happened</h2>
            <Stream ctx={ctx} items={graph.items} />
          </section>
        ) : (
          <>
            <section className="ev-col ev-heard">
              <h2 className="section-label">Heard</h2>
              {heard.length ? <Stream ctx={ctx} items={heard} /> : <div className="empty">Nothing was said.</div>}
            </section>
            <section className="ev-col ev-seen">
              <h2 className="section-label">Seen</h2>
              {seen.length ? <Stream ctx={ctx} items={seen} /> : <div className="empty">No photos or video.</div>}
            </section>
          </>
        )}

        <aside className="ev-aside">
          {picked && <Selected ctx={ctx} item={picked} onOpen={onOpenSession} />}
          <h2 className="section-label">
            {picked ? "Connects to" : "Connections"}
            <span className="section-count">{shownLinks.length}</span>
          </h2>
          {shownLinks.length === 0 ? (
            <p className="ev-hint">
              {picked ? "Nothing linked." : "No links."}
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
                      <Connection
                        key={`${l.from_artifact_id}-${l.to_artifact_id}-${l.relation}`}
                        ctx={ctx}
                        link={l}
                        detail={!!picked}
                      />
                    ))}
                  </ul>
                </div>
              )
            })
          )}
        </aside>
      </div>
      <TextSeen moment={moment} />
    </div>
  )
}
