import { type MouseEvent, useCallback, useEffect, useState } from "react"
import { Link, useNavigate } from "react-router"
import { api } from "../api/client"
import { ACTION, type Automation, TRIGGER, conditions, fieldNames, setEnabled, when } from "../automations"
import { dayLabel } from "../format"
import { isRefusal } from "../readOnly"
import PageHead from "./PageHead"
import Switch from "./Switch"

/** Every automation as a row: what triggers it, what it checks, what it does. */
export default function AutomationsPage() {
  const navigate = useNavigate()
  const [rules, setRules] = useState<Automation[] | null>(null)
  const [failed, setFailed] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)

  const load = useCallback(async () => {
    const { data } = await api.GET("/v1/automations")
    setFailed(!data)
    if (data) setRules(data)
  }, [])
  useEffect(() => {
    void load()
  }, [load])

  const toggle = async (a: Automation) => {
    const { data: updated, error } = await setEnabled(a, !a.enabled)
    setNotice(updated || isRefusal(error) ? null : `Couldn't turn “${a.name}” ${a.enabled ? "off" : "on"}.`)
    if (updated) setRules((prev) => (prev ?? []).map((r) => (r.id === a.id ? updated : r)))
  }

  return (
    <div className="automations-page">
      <PageHead title="Automations" meta="Trigger effects, with structured insights.">
        <Link className="btn primary slim" to="/automations/new">
          New automation
        </Link>
      </PageHead>

      {notice && <div className="banner error">{notice}</div>}
      {failed && !rules ? (
        <div className="banner error">
          Couldn't load your automations.{" "}
          <button className="link-btn" onClick={() => void load()}>
            Try again
          </button>
        </div>
      ) : !rules ? (
        <div className="skeleton" style={{ height: 160 }} />
      ) : rules.length === 0 ? (
        <div className="empty">
          No automations yet. An automation checks each new moment and, when it matches, fills in fields you
          choose. It only runs on moments made after you create it.
        </div>
      ) : (
        <div className="tool-table-wrap">
          <table className="tool-table auto-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Trigger</th>
                <th>Conditions</th>
                <th>Action</th>
                <th className="num">Matches</th>
                <th>Last match</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {rules.map((a) => {
                const conds = conditions(a)
                const fields = fieldNames(a)
                return (
                  <tr
                    key={a.id}
                    className={a.enabled ? "" : "off"}
                    onClick={(e: MouseEvent) => {
                      // Leave modified clicks and text selection alone; the name link covers keyboards.
                      if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return
                      if (window.getSelection()?.toString()) return
                      navigate(`/automations/${a.id}`)
                    }}
                  >
                    <td className="auto-name">
                      <Link to={`/automations/${a.id}`} onClick={(e) => e.stopPropagation()}>
                        {a.name}
                      </Link>
                    </td>
                    <td>
                      <span className="kind-tag">{TRIGGER[a.trigger]}</span>
                    </td>
                    <td>
                      {conds.length ? (
                        <div className="auto-conds">
                          {conds.map((c) => (
                            <span key={c.kind} className="cond" title={`${c.kind}: ${c.value}`}>
                              <span className="cond-kind">{c.kind}</span>
                              <span className="cond-value">{c.value}</span>
                            </span>
                          ))}
                        </div>
                      ) : (
                        <span className="row-dim">Every moment</span>
                      )}
                    </td>
                    <td>
                      <span className="kind-tag">{ACTION.extract}</span>
                      <span className="auto-sub" title={fields.join(", ")}>
                        {fields.length} field{fields.length === 1 ? "" : "s"}
                      </span>
                    </td>
                    <td className="num">
                      {a.matches}
                      {a.failures > 0 && <span className="auto-sub auto-failed">{a.failures} failed</span>}
                    </td>
                    <td
                      className="row-dim nowrap"
                      title={a.last_match_at ? when(a.last_match_at) : undefined}
                    >
                      {a.last_match_at ? dayLabel(a.last_match_at) : "—"}
                    </td>
                    <td className="auto-status">
                      <Switch
                        on={a.enabled}
                        label={a.name}
                        onToggle={() => void toggle(a)}
                      />
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
