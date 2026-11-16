import { useCallback, useEffect, useState } from "react"
import { Link, useNavigate } from "react-router"
import { api } from "../api/client"
import {
  ACTION,
  type Automation,
  type AutomationRun,
  type AutomationWrite,
  EMPTY,
  TRIGGER,
  conditions,
  problem,
  setEnabled,
  toDraft,
  when,
} from "../automations"
import { isRefusal } from "../readOnly"
import { useSearchParam } from "../useParam"
import AutomationEditor from "./AutomationEditor"
import Switch from "./Switch"

type Filter = "all" | AutomationRun["status"]
const RUN_LIMIT = 200
const FILTERS: [Filter, string][] = [
  ["all", "All"],
  ["matched", "Matched"],
  ["skipped", "Skipped"],
  ["failed", "Failed"],
]

/** A new automation: just the editor, then on to its page. */
export function NewAutomation() {
  const navigate = useNavigate()
  const save = async (body: AutomationWrite) => {
    const { data, error } = await api.POST("/v1/automations", { body })
    if (!data) return problem(error)
    navigate(`/automations/${data.id}`, { replace: true })
    return null
  }
  return (
    <div className="automation-view">
      <Link className="crumb" to="/automations">
        ← Automations
      </Link>
      <h1 className="page-title">New automation</h1>
      <AutomationEditor initial={EMPTY} onSave={save} onCancel={() => navigate("/automations")} />
    </div>
  )
}

/** One automation: its definition, controls, and every run it has made. */
export default function AutomationView({ id }: { id: string }) {
  const navigate = useNavigate()
  const [edit, setEdit] = useSearchParam("edit", "")
  const [rule, setRule] = useState<Automation | null>(null)
  const [runs, setRuns] = useState<AutomationRun[] | null>(null)
  const [status, setStatus] = useState<number | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [filter, setFilter] = useState<Filter>("all")

  const load = useCallback(async () => {
    try {
      const { data, response } = await api.GET("/v1/automations/{automation_id}", {
        params: { path: { automation_id: id } },
      })
      setStatus(data ? null : response.status)
      if (data) setRule(data)
    } catch {
      setStatus(0)
    }
  }, [id])
  useEffect(() => {
    void load()
  }, [load])

  // Filtered on the server: skipped runs pile up, one per moment, and would crowd out the rest.
  useEffect(() => {
    let live = true
    setRuns(null)
    const query = {
      automation_id: id,
      limit: RUN_LIMIT,
      ...(filter === "all" ? {} : { status: filter }),
    }
    void api
      .GET("/v1/automations/runs", { params: { query } })
      .then(({ data }) => live && setRuns(data ?? []))
      .catch(() => live && setRuns([]))
    return () => {
      live = false
    }
  }, [id, filter])

  if (!rule)
    return (
      <div className="automation-view">
        <Link className="crumb" to="/automations">
          ← Automations
        </Link>
        {status === null ? (
          <div className="skeleton" style={{ height: 200 }} />
        ) : (
          <div className="banner error">
            {status === 404 ? "This automation no longer exists." : "Couldn't load this automation. "}
            {status !== 404 && (
              <button className="link-btn" onClick={() => void load()}>
                Try again
              </button>
            )}
          </div>
        )}
      </div>
    )

  const save = async (body: AutomationWrite) => {
    const { data, error } = await api.PUT("/v1/automations/{automation_id}", {
      params: { path: { automation_id: id } },
      body,
    })
    if (!data) return problem(error)
    setRule(data)
    setEdit("")
    return null
  }

  const toggle = async () => {
    const { data: updated, error } = await setEnabled(rule, !rule.enabled)
    setNotice(updated || isRefusal(error) ? null : `Couldn't turn it ${rule.enabled ? "off" : "on"}.`)
    if (updated) setRule(updated)
  }

  const remove = async () => {
    if (!window.confirm(`Delete “${rule.name}”? Its runs go with it.`)) return
    const { response, error } = await api.DELETE("/v1/automations/{automation_id}", {
      params: { path: { automation_id: id } },
    })
    if (!response.ok) return setNotice(isRefusal(error) ? null : "Couldn't delete it.")
    navigate("/automations", { replace: true })
  }

  const conds = conditions(rule)
  const fields = rule.actions.flatMap((x) => x.fields)
  const counts: Record<Filter, number> = {
    all: rule.matches + rule.skipped + rule.failures,
    matched: rule.matches,
    skipped: rule.skipped,
    failed: rule.failures,
  }
  const shown = runs ?? []

  return (
    <div className="automation-view">
      <Link className="crumb" to="/automations">
        ← Automations
      </Link>
      <header className="auto-head">
        <h1 className="page-title">{rule.name}</h1>
        {!edit && (
          <Switch on={rule.enabled} label={rule.name} onToggle={() => void toggle()} />
        )}
        {!edit && (
          <span className="page-actions">
            <button className="btn ghost slim" onClick={() => setEdit("1")}>
              Edit
            </button>
            <button className="btn ghost slim" onClick={() => void remove()}>
              Delete
            </button>
          </span>
        )}
      </header>
      {notice && <div className="banner error">{notice}</div>}

      {edit ? (
        <AutomationEditor initial={toDraft(rule)} onSave={save} onCancel={() => setEdit("")} />
      ) : (
        <div className="auto-flow">
          <section className="auto-step">
            <h2 className="step-label">Trigger</h2>
            <span className="kind-tag">{TRIGGER[rule.trigger]}</span>
            <p className="auto-sub">For moments created since {when(rule.active_since)}.</p>
          </section>
          <section className="auto-step">
            <h2 className="step-label">Conditions</h2>
            {conds.length ? (
              <dl className="kv">
                {conds.map((c) => (
                  <div key={c.kind}>
                    <dt>{c.kind}</dt>
                    <dd>{c.value}</dd>
                  </div>
                ))}
              </dl>
            ) : (
              <p className="auto-sub">None: runs on every moment.</p>
            )}
          </section>
          <section className="auto-step">
            <h2 className="step-label">Action</h2>
            <span className="kind-tag">{ACTION.extract}</span>
            <dl className="kv">
              {fields.map((f) => (
                <div key={f.name}>
                  <dt>{f.name}</dt>
                  <dd className={f.description ? "" : "row-dim"}>{f.description || "No description"}</dd>
                </div>
              ))}
            </dl>
          </section>
        </div>
      )}

      <section className="auto-runs">
        <div className="auto-runs-head">
          <h2 className="section-label">Runs</h2>
          <div className="mode-toggle slim-toggle" role="group" aria-label="Show runs">
            {FILTERS.map(([key, label]) => (
              <button
                key={key}
                className={`mode ${filter === key ? "active" : ""}`}
                aria-pressed={filter === key}
                onClick={() => setFilter(key)}
              >
                {label} <span className="seg-count">{counts[key]}</span>
              </button>
            ))}
          </div>
        </div>
        {!runs ? (
          <div className="skeleton" style={{ height: 80 }} />
        ) : shown.length === 0 ? (
          <p className="auto-sub">
            {counts.all
              ? "No runs with this result."
              : "No runs yet. It runs on each new moment while it's on."}
          </p>
        ) : (
          <>
            <div className="tool-table-wrap">
              <table className="tool-table">
                <thead>
                  <tr>
                    <th>Moment</th>
                    <th>When</th>
                    <th>Result</th>
                    <th>Output</th>
                  </tr>
                </thead>
                <tbody>
                  {shown.map((run) => (
                    <RunRow key={run.id} run={run} />
                  ))}
                </tbody>
              </table>
            </div>
            {shown.length === RUN_LIMIT && <p className="auto-sub">Showing the latest {RUN_LIMIT}.</p>}
          </>
        )}
      </section>
    </div>
  )
}

function RunRow({ run }: { run: AutomationRun }) {
  const filled = Object.entries(run.fields).filter(([, v]) => v)
  return (
    <tr className={`run-${run.status}`}>
      <td className="auto-name">
        <Link to={`/moments/${run.moment_id}`}>{run.moment_title || "Untitled moment"}</Link>
      </td>
      <td className="row-dim nowrap">{when(run.moment_start_at)}</td>
      <td>
        <span className={`result ${run.status}`}>{run.status}</span>
      </td>
      <td className="auto-output">
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
      </td>
    </tr>
  )
}
