import { useCallback, useState } from "react"
import { Link } from "react-router"
import { api, type SessionRead } from "../api/client"
import { listMoments, type MomentRead } from "../api/moments"
import { type Automation, type AutomationRun, setEnabled, when } from "../automations"
import { dayKey, fmtLongDay } from "../format"
import { usePoll } from "../hooks/usePoll"
import { isUnnamed, joinNames } from "../moments"
import { BuildingNote, MomentEntry } from "./MomentsPage"
import PageHead from "./PageHead"
import Switch from "./Switch"

const POLL_MS = 30_000
const RUNS = 30
const MOMENTS = 5

/** The signed-in home: the latest moments, the rules running, and what they caught. */
export default function Overview() {
  const [runs, setRuns] = useState<AutomationRun[] | null>(null)
  const [rules, setRules] = useState<Automation[] | null>(null)
  const [moments, setMoments] = useState<MomentRead[] | null>(null)
  const [building, setBuilding] = useState(0)
  const [live, setLive] = useState<SessionRead[]>([])

  const refresh = useCallback(async () => {
    const [r, a, m, s] = await Promise.all([
      api.GET("/v1/automations/runs", { params: { query: { limit: RUNS } } }).catch(() => null),
      api.GET("/v1/automations").catch(() => null),
      listMoments(null, MOMENTS),
      api.GET("/v1/sessions", { params: { query: { limit: 10, offset: 0 } } }).catch(() => null),
    ])
    setRuns(r?.data ?? [])
    setRules(a?.data ?? [])
    if (m.data) {
      setMoments(m.data.items)
      setBuilding(m.data.building)
    } else setMoments((prev) => prev ?? [])
    if (s?.data) setLive(s.data.items.filter((x) => x.is_open))
  }, [])
  usePoll(refresh, POLL_MS)

  const toggle = async (a: Automation) => {
    const { data } = await setEnabled(a, !a.enabled)
    if (data) setRules((prev) => (prev ?? []).map((r) => (r.id === a.id ? data : r)))
  }

  const today = dayKey(new Date().toISOString())
  const todays = (moments ?? []).filter((m) => dayKey(m.start_at) === today)
  const people = [
    ...new Set(todays.flatMap((m) => m.speakers.filter((sp) => !isUnnamed(sp)).map((sp) => sp.name))),
  ]
  const digest = [
    todays.length ? `${todays.length} moment${todays.length === 1 ? "" : "s"} today` : null,
    people.length ? `with ${joinNames(people, 3)}` : null,
  ].filter(Boolean)

  return (
    <div className="overview">
      <PageHead title={fmtLongDay(new Date().toISOString())} meta={digest.join(" · ") || null} />
      <BuildingNote live={live} building={building} />

      <section className="overview-section">
        <div className="overview-head">
          <h2 className="section-label">Latest moments</h2>
          <Link to="/moments" className="overview-more">
            All
          </Link>
        </div>
        {!moments ? (
          <div className="skeleton" style={{ height: 240 }} />
        ) : moments.length === 0 ? (
          <div className="overview-empty">No moments yet.</div>
        ) : (
          <div className="moment-list">
            {moments.map((m) => (
              <MomentEntry key={m.id} moment={m} />
            ))}
          </div>
        )}
      </section>

      <div className="overview-grid">
        <section className="overview-section home-insights">
          <div className="overview-head">
            <h2 className="section-label">Actions triggered</h2>
            <Link to="/automations" className="overview-more">
              All
            </Link>
          </div>
          {!runs ? (
            <div className="skeleton" style={{ height: 140 }} />
          ) : runs.length === 0 ? (
            <div className="overview-empty">
              Nothing caught yet.{" "}
              {rules?.length ? null : <Link to="/automations/new">Set up an automation</Link>}
            </div>
          ) : (
            <div className="tool-table-wrap">
              <table className="tool-table action-table">
                <thead>
                  <tr>
                    <th>Automation</th>
                    <th>Moment</th>
                    <th>When</th>
                    <th>Result</th>
                    <th>Output</th>
                  </tr>
                </thead>
                <tbody>
                  {runs.map((run) => (
                    <ActionRow key={run.id} run={run} />
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>

        <aside className="overview-rail">
          <section className="overview-section">
            <div className="overview-head">
              <h2 className="section-label">Automations</h2>
              <Link to="/automations/new" className="overview-more">
                New
              </Link>
            </div>
            {!rules ? (
              <div className="skeleton" style={{ height: 120 }} />
            ) : rules.length === 0 ? (
              <div className="overview-empty">None yet.</div>
            ) : (
              <ul className="rule-list">
                {rules.map((a) => (
                  <li key={a.id} className={a.enabled ? "" : "off"}>
                    <Link to={`/automations/${a.id}`} className="rule-name">
                      {a.name}
                    </Link>
                    <span className="rule-count" title={a.last_match_at ? `Last ${when(a.last_match_at)}` : undefined}>
                      {a.matches}
                    </span>
                    <Switch on={a.enabled} label={a.name} onToggle={() => void toggle(a)} />
                  </li>
                ))}
              </ul>
            )}
          </section>
        </aside>
      </div>
    </div>
  )
}

/** One run as a table row; every cell scrolls on its own when its content runs long. */
function ActionRow({ run }: { run: AutomationRun }) {
  const filled = Object.entries(run.fields).filter(([, v]) => v)
  return (
    <tr className={`run-${run.status}`}>
      <td>
        <div className="cell-scroll">
          <Link to={`/automations/${run.automation_id}`}>{run.automation_name}</Link>
        </div>
      </td>
      <td className="auto-name">
        <div className="cell-scroll">
          <Link to={`/moments/${run.moment_id}`}>{run.moment_title || "Untitled moment"}</Link>
        </div>
      </td>
      <td className="row-dim nowrap">{when(run.moment_start_at)}</td>
      <td>
        <span className={`result ${run.status}`}>{run.status === "matched" ? "fired" : run.status}</span>
      </td>
      <td className="auto-output">
        <div className="cell-scroll">
          {run.status === "failed" ? (
            <span className="auto-failed">{run.error}</span>
          ) : filled.length ? (
            <dl className="kv compact">
              {filled.map(([k, v]) => (
                <div key={k}>
                  <dt>{k}</dt>
                  <dd>{v}</dd>
                </div>
              ))}
            </dl>
          ) : (
            <span className="row-dim">{run.reason}</span>
          )}
        </div>
      </td>
    </tr>
  )
}
