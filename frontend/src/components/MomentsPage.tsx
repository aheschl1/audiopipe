import { useCallback, useRef, useState } from "react"
import { Link } from "react-router"
import { api, type SessionRead } from "../api/client"
import { listMoments, type MomentCover, type MomentRead } from "../api/moments"
import { clock, dayKey, dayLabel, fmtLongDay, fmtSpan } from "../format"
import { usePoll } from "../hooks/usePoll"
import { useSearchParam } from "../useParam"
import { heardWith, isUnnamed, joinNames } from "../moments"
import { artifactUrl } from "../visual"
import PageHead from "./PageHead"

const POLL_MS = 30_000
// A closed recording newer than every moment is still being written up.
const PENDING_WINDOW_MS = 6 * 3600_000

type Day = { key: string; iso: string; moments: MomentRead[] }

function groupByDay(moments: MomentRead[]): Day[] {
  const days: Day[] = []
  for (const moment of moments) {
    const key = dayKey(moment.start_at)
    const last = days[days.length - 1]
    if (last?.key === key) last.moments.push(moment)
    else days.push({ key, iso: moment.start_at, moments: [moment] })
  }
  return days
}

/** Exclusive upper bound for a `YYYY-MM-DD` jump: the next local midnight. */
function endOfDay(day: string): string | null {
  const [y, m, d] = day.split("-").map(Number)
  if (!y || !m || !d) return null
  return new Date(y, m - 1, d + 1).toISOString()
}

function todayInput(): string {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`
}

function unique(lists: string[][]): string[] {
  return [...new Set(lists.flat())]
}

function CoverMosaic({
  covers,
}: {
  covers: MomentCover[]
}) {
  const shown = covers.slice(0, 4)
  if (!shown.length) return null
  return (
    <span className={`cover n${shown.length}`} aria-hidden="true">
      {shown.map((c) => (
        <span key={c.artifact_id} className="cover-cell">
          <img src={artifactUrl(c.artifact_id)} alt="" loading="lazy" />
        </span>
      ))}
    </span>
  )
}

function MomentEntry({
  moment,
}: {
  moment: MomentRead
}) {
  const { utterances, photos, frames } = moment.counts
  const context = [
    heardWith(moment.speakers),
    moment.places.length ? `at ${joinNames(moment.places, 2)}` : null,
  ].filter(Boolean)
  const tally = [
    utterances ? `${utterances} lines` : null,
    photos ? `${photos} photo${photos === 1 ? "" : "s"}` : null,
    frames ? `${frames} frame${frames === 1 ? "" : "s"}` : null,
  ].filter(Boolean)

  return (
    <Link to={`/moments/${moment.id}`} className={`moment ${moment.cover.length ? "has-cover" : ""}`}>
      <span className="moment-when">
        <span className="moment-start">{clock(moment.start_at)}</span>
        <span className="moment-span">{fmtSpan(moment.start_at, moment.end_at)}</span>
      </span>
      <span className="moment-body">
        <span className="moment-title">{moment.title || "Untitled moment"}</span>
        {context.length > 0 && <span className="moment-context">{context.join(" · ")}</span>}
        {moment.summary && <span className="moment-summary">{moment.summary}</span>}
        {tally.length > 0 && <span className="moment-tally">{tally.join(" · ")}</span>}
      </span>
      <CoverMosaic covers={moment.cover} />
    </Link>
  )
}

/** A day's heading: the date, a one-line digest, and the slot the day's story will fill. */
function DayHead({ day, story }: { day: Day; story?: string | null }) {
  const label = dayLabel(day.iso)
  const long = fmtLongDay(day.iso)
  const people = unique(
    day.moments.map((m) => m.speakers.filter((sp) => !isUnnamed(sp)).map((sp) => sp.name)),
  )
  const places = unique(day.moments.map((m) => m.places))
  const n = day.moments.length
  const digest = [
    n ? `${n} moment${n === 1 ? "" : "s"}` : null,
    people.length ? `with ${joinNames(people, 4)}` : null,
    places.length ? joinNames(places, 3) : null,
  ].filter(Boolean)
  return (
    <header className="day-head">
      <h2 className="day-title">
        {label}
        {label !== long && <span className="day-date">{long}</span>}
      </h2>
      {digest.length > 0 && <div className="day-digest">{digest.join(" · ")}</div>}
      {story && <p className="day-story">{story}</p>}
    </header>
  )
}

/** Recording happening now, or finished but not yet written up as a moment. */
function BuildingNote({ live, pending }: { live: SessionRead[]; pending: SessionRead[] }) {
  if (live.length) {
    const device = live[0]!.device
    return (
      <div className="building">
        <span className="badge live">recording now{device ? ` on ${device}` : ""}</span>
        <span className="row-dim">It becomes a moment a few minutes after recording stops.</span>
      </div>
    )
  }
  if (pending.length) {
    return (
      <div className="building">
        <span className="spinner" aria-hidden="true" />
        <span className="row-dim">
          Writing up your last {pending.length === 1 ? "recording" : `${pending.length} recordings`}
          {" "} - it will appear here.
        </span>
      </div>
    )
  }
  return null
}

/** The home page: days of moments, newest first. A day jump remounts the feed. */
export default function MomentsPage() {
  const [day, setDay] = useSearchParam("day", "")
  const jump = (value: string) => setDay(value && value < todayInput() ? value : "")
  return <MomentFeed key={day} day={day} onJump={jump} />
}

function MomentFeed({ day, onJump }: { day: string; onJump: (day: string) => void }) {
  const anchor = day ? endOfDay(day) : null
  const [moments, setMoments] = useState<MomentRead[] | null>(null)
  const [next, setNext] = useState<string | null>(null)
  const [status, setStatus] = useState<number | null>(null)
  const [loadingMore, setLoadingMore] = useState(false)
  const [sessions, setSessions] = useState<SessionRead[] | null>(null)
  const pagedOlder = useRef(false)

  // The poll refreshes the newest page; older pages loaded via "earlier" stay.
  const refresh = useCallback(async () => {
    const [page, recent] = await Promise.all([
      listMoments(anchor),
      anchor
        ? Promise.resolve(null)
        : api.GET("/v1/sessions", { params: { query: { limit: 10, offset: 0 } } }),
    ])
    if (recent?.data) setSessions(recent.data.items)
    if (!page.data) {
      setStatus(page.status)
      return
    }
    const fresh = page.data
    setStatus(null)
    if (!pagedOlder.current) {
      setNext(fresh.next_before)
      setMoments(fresh.items)
      return
    }
    setMoments((prev) => {
      const ids = new Set(fresh.items.map((m) => m.id))
      return [...fresh.items, ...(prev ?? []).filter((m) => !ids.has(m.id))]
    })
  }, [anchor])
  usePoll(refresh, POLL_MS)

  const loadEarlier = async () => {
    if (!next) return
    setLoadingMore(true)
    const page = await listMoments(next)
    setLoadingMore(false)
    if (!page.data) return
    pagedOlder.current = true
    setNext(page.data.next_before)
    setMoments((prev) => {
      const seen = new Set((prev ?? []).map((m) => m.id))
      return [...(prev ?? []), ...page.data.items.filter((m) => !seen.has(m.id))]
    })
  }

  const live = sessions?.filter((s) => s.is_open) ?? []
  const newestEnd = moments?.[0]?.end_at ?? ""
  const pending =
    sessions?.filter(
      (s) =>
        !s.is_open &&
        s.ended_at &&
        s.ended_at > newestEnd &&
        Date.now() - Date.parse(s.ended_at) < PENDING_WINDOW_MS,
    ) ?? []

  const days = groupByDay(moments ?? [])
  const todayKey = dayKey(new Date().toISOString())
  // Today always gets a heading while something is being recorded or written up.
  if (!anchor && (live.length || pending.length) && days[0]?.key !== todayKey)
    days.unshift({ key: todayKey, iso: new Date().toISOString(), moments: [] })

  return (
    <div className="moments-page">
      <PageHead
        title="Moments"
        meta={
          anchor
            ? `Showing ${fmtLongDay(new Date(Date.parse(anchor) - 1).toISOString())} and earlier`
            : "Your days, as they happened."
        }
      >
        {anchor && (
          <button className="btn ghost slim" onClick={() => onJump("")}>
            back to today
          </button>
        )}
        <label className="jump">
          <span className="sr-only">jump to a day</span>
          <input
            type="date"
            className="input slim"
            max={todayInput()}
            value={day || todayInput()}
            onChange={(e) => onJump(e.target.value)}
          />
        </label>
      </PageHead>

      {status !== null && !moments ? (
        <div className="banner error">
          {status === 404
            ? "Moments aren't available on this server yet. "
            : "Couldn't load your moments. "}
          <Link to="/sessions">Browse recordings</Link>
          {status !== 404 && (
            <>
              {" or "}
              <button className="link-btn" onClick={() => void refresh()}>
                try again
              </button>
            </>
          )}
        </div>
      ) : !moments ? (
        <div className="day">
          <div className="skeleton" style={{ height: 28, width: 160, marginBottom: 24 }} />
          {[120, 120, 120].map((height, i) => (
            <div key={i} className="skeleton" style={{ height, marginBottom: 2 }} />
          ))}
        </div>
      ) : days.length === 0 ? (
        <div className="empty">
          {anchor ? (
            "Nothing was recorded on or before this day."
          ) : sessions && sessions.length === 0 ? (
            "Nothing recorded yet. Record from your phone or glasses and your day shows up here."
          ) : (
            <>
              No moments yet. Recordings become moments a few minutes after they stop.{" "}
              <Link to="/sessions">Browse recordings</Link>
            </>
          )}
        </div>
      ) : (
        days.map((d) => (
          <section key={d.key} className="day">
            <DayHead day={d} />
            {d.key === todayKey && !anchor && <BuildingNote live={live} pending={pending} />}
            <div className="moment-list">
              {d.moments.map((m) => (
                <MomentEntry key={m.id} moment={m} />
              ))}
            </div>
          </section>
        ))
      )}

      {status !== null && moments && (
        <div className="banner error">Couldn't refresh. Showing what was loaded.</div>
      )}
      {next && moments && (
        <button className="btn ghost full" onClick={() => void loadEarlier()} disabled={loadingMore}>
          {loadingMore ? "loading…" : "show earlier days"}
        </button>
      )}
    </div>
  )
}
