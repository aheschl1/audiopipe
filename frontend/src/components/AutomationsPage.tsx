import { useCallback, useEffect, useState } from "react"
import { Link } from "react-router"
import { api, type Schemas } from "../api/client"
import { clock, fmtLongDay } from "../format"
import { useReadOnly } from "../readOnly"
import PageHead from "./PageHead"

type Automation = Schemas["AutomationRead"]
type AutomationWrite = Schemas["AutomationWrite"]
type Run = Schemas["AutomationRunRead"]

/** The editor's flat form of a rule; lists are comma- or line-separated text. */
type Draft = {
  name: string
  enabled: boolean
  people: string
  topics: string
  keywords: string
  minMinutes: string
  ask: string
  fields: { name: string; description: string }[]
}

const EMPTY: Draft = {
  name: "",
  enabled: true,
  people: "",
  topics: "",
  keywords: "",
  minMinutes: "",
  ask: "",
  fields: [{ name: "", description: "" }],
}

const list = (text: string, sep = /[,\n]/) =>
  text
    .split(sep)
    .map((s) => s.trim())
    .filter(Boolean)

function toDraft(a: Automation): Draft {
  const c = a.conditions
  return {
    name: a.name,
    enabled: a.enabled,
    people: c.people.join(", "),
    topics: c.topics.join(", "),
    keywords: c.keywords.join(", "),
    minMinutes: c.min_minutes == null ? "" : String(c.min_minutes),
    ask: c.ask ?? "",
    fields: a.actions.flatMap((x) => x.fields.map((f) => ({ name: f.name, description: f.description }))),
  }
}

function toWrite(d: Draft): AutomationWrite {
  const fields = d.fields.filter((f) => f.name.trim())
  const minutes = d.minMinutes.trim() ? Number(d.minMinutes) : null
  return {
    name: d.name.trim(),
    enabled: d.enabled,
    trigger: "moment",
    conditions: {
      people: list(d.people),
      topics: list(d.topics),
      keywords: list(d.keywords),
      min_minutes: minutes,
      ask: d.ask.trim() || null,
    },
    actions: fields.length ? [{ type: "extract", fields }] : [],
  }
}

/** The stored rule as a write body, unchanged: a toggle mustn't reparse it. */
function asWrite(a: Automation): AutomationWrite {
  const { name, enabled, trigger, conditions, actions } = a
  return { name, enabled, trigger, conditions, actions }
}

/** One line saying what the rule does, e.g. "With Sam, if “a deadline is set?”: fill in due". */
function describe(a: Automation): string {
  const c = a.conditions
  const when = [
    c.people.length ? `with ${c.people.join(" or ")}` : null,
    c.topics.length ? `about ${c.topics.join(" or ")}` : null,
    c.keywords.length ? `mentioning ${c.keywords.map((k) => `“${k}”`).join(" or ")}` : null,
    c.min_minutes ? `at least ${c.min_minutes} min` : null,
    c.ask ? `if “${c.ask}”` : null,
  ].filter(Boolean)
  const fields = a.actions.flatMap((x) => x.fields.map((f) => f.name))
  const head = when.length ? when.join(", ") : "Every moment"
  return `${head[0]!.toUpperCase()}${head.slice(1)}: fill in ${fields.join(", ")}`
}

/** FastAPI's 422 detail as one readable line. */
function problem(error: unknown): string {
  const detail = (error as { detail?: unknown } | undefined)?.detail
  if (typeof detail === "string") return detail
  if (Array.isArray(detail) && detail.length) {
    const first = detail[0] as { loc?: unknown[]; msg?: string }
    const where = (first.loc ?? []).filter((p) => p !== "body").join(" › ")
    return where ? `${where}: ${first.msg}` : (first.msg ?? "Invalid automation.")
  }
  return "Couldn't save the automation."
}

function Editor({
  initial,
  onSave,
  onCancel,
}: {
  initial: Draft
  onSave: (body: AutomationWrite) => Promise<string | null>
  onCancel: () => void
}) {
  const [d, setD] = useState(initial)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const set = <K extends keyof Draft>(key: K, value: Draft[K]) => setD((prev) => ({ ...prev, [key]: value }))
  const setField = (i: number, patch: Partial<Draft["fields"][number]>) =>
    set("fields", d.fields.map((f, j) => (j === i ? { ...f, ...patch } : f)))

  const submit = async () => {
    const body = toWrite(d)
    if (!body.name) return setError("Give it a name.")
    if (!body.actions.length) return setError("Add at least one field to fill in.")
    if (d.minMinutes.trim() && !Number.isFinite(Number(d.minMinutes)))
      return setError("Minimum length must be a number of minutes.")
    setSaving(true)
    const failure = await onSave(body)
    setSaving(false)
    setError(failure)
  }

  return (
    <form
      className="auto-editor"
      onSubmit={(e) => {
        e.preventDefault()
        void submit()
      }}
    >
      <label className="auto-row">
        <span className="section-label">Name</span>
        <input className="input" value={d.name} maxLength={200} autoFocus onChange={(e) => set("name", e.target.value)} />
      </label>

      <fieldset className="auto-group">
        <legend className="section-label">When a moment</legend>
        <div className="auto-grid">
          <label className="auto-row">
            <span className="auto-hint">Includes any of these people</span>
            <input className="input" placeholder="Sam, Ana" value={d.people} onChange={(e) => set("people", e.target.value)} />
          </label>
          <label className="auto-row">
            <span className="auto-hint">Is about any of these topics</span>
            <input className="input" placeholder="hiring, travel" value={d.topics} onChange={(e) => set("topics", e.target.value)} />
          </label>
          <label className="auto-row">
            <span className="auto-hint">Mentions any of these words</span>
            <input className="input" placeholder="deadline, invoice" value={d.keywords} onChange={(e) => set("keywords", e.target.value)} />
          </label>
          <label className="auto-row">
            <span className="auto-hint">Lasts at least (minutes)</span>
            <input
              className="input"
              type="number"
              min={0}
              step="any"
              value={d.minMinutes}
              onChange={(e) => set("minMinutes", e.target.value)}
            />
          </label>
        </div>
        <label className="auto-row">
          <span className="auto-hint">And the model answers yes to</span>
          <textarea
            className="input"
            rows={2}
            maxLength={1000}
            placeholder="Was a deadline or due date agreed?"
            value={d.ask}
            onChange={(e) => set("ask", e.target.value)}
          />
        </label>
        <p className="auto-hint">Leave everything blank to run on every moment.</p>
      </fieldset>

      <fieldset className="auto-group">
        <legend className="section-label">Then fill in these fields</legend>
        <p className="auto-hint">The model fills each from the moment, or leaves it blank. They show on the moment.</p>
        {d.fields.map((f, i) => (
          <div key={i} className="auto-field">
            <input
              className="input slim"
              placeholder="Field name"
              aria-label={`Field ${i + 1} name`}
              maxLength={60}
              value={f.name}
              onChange={(e) => setField(i, { name: e.target.value })}
            />
            <input
              className="input slim"
              placeholder="What to put in it (optional)"
              aria-label={`Field ${i + 1} description`}
              maxLength={500}
              value={f.description}
              onChange={(e) => setField(i, { description: e.target.value })}
            />
            <button
              type="button"
              className="btn ghost slim"
              aria-label={`Remove ${f.name || "field"}`}
              onClick={() => set("fields", d.fields.filter((_, j) => j !== i))}
            >
              Remove
            </button>
          </div>
        ))}
        {d.fields.length < 20 && (
          <button
            type="button"
            className="btn ghost slim auto-add"
            onClick={() => set("fields", [...d.fields, { name: "", description: "" }])}
          >
            Add a field
          </button>
        )}
      </fieldset>

      {error && <div className="banner error">{error}</div>}
      <div className="auto-actions">
        <label className="auto-check">
          <input type="checkbox" checked={d.enabled} onChange={(e) => set("enabled", e.target.checked)} />
          On
        </label>
        <button type="button" className="btn ghost slim" disabled={saving} onClick={onCancel}>
          Cancel
        </button>
        <button className="btn primary slim" disabled={saving}>
          {saving ? "Saving…" : "Save"}
        </button>
      </div>
    </form>
  )
}

function RunRow({ run }: { run: Run }) {
  const filled = Object.entries(run.fields).filter(([, v]) => v)
  return (
    <li className="auto-run">
      <span className="auto-run-when">
        {fmtLongDay(run.moment_start_at)} · {clock(run.moment_start_at)}
      </span>
      <span className="auto-run-body">
        <span>
          <span className="auto-run-rule">{run.automation_name}</span>
          {" on "}
          <Link to={`/moments/${run.moment_id}`}>{run.moment_title || "Untitled moment"}</Link>
          {run.status === "failed" && <span className="auto-failed"> · failed</span>}
        </span>
        {run.status === "failed" ? (
          <span className="row-dim">{run.error}</span>
        ) : filled.length ? (
          <span className="chips">
            {filled.map(([k, v]) => (
              <span key={k} className="chip">
                {k}: {v}
              </span>
            ))}
          </span>
        ) : (
          run.reason && <span className="row-dim">{run.reason}</span>
        )}
      </span>
    </li>
  )
}

/** Rules that run on each new moment: when it matches, the model fills in fields. */
export default function AutomationsPage() {
  const readOnly = useReadOnly()
  const [rules, setRules] = useState<Automation[] | null>(null)
  const [runs, setRuns] = useState<Run[]>([])
  const [failed, setFailed] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  // "new", an automation id being edited, or null.
  const [editing, setEditing] = useState<string | null>(null)

  const load = useCallback(async () => {
    const [r, x] = await Promise.all([
      api.GET("/v1/automations"),
      api.GET("/v1/automations/runs", { params: { query: { limit: 30 } } }),
    ])
    setFailed(!r.data)
    if (r.data) setRules(r.data)
    if (x.data) setRuns(x.data)
  }, [])
  useEffect(() => {
    void load()
  }, [load])

  const save = async (id: string | null, body: AutomationWrite): Promise<string | null> => {
    const { data, error } = id
      ? await api.PUT("/v1/automations/{automation_id}", { params: { path: { automation_id: id } }, body })
      : await api.POST("/v1/automations", { body })
    if (!data) return problem(error)
    setRules((prev) => (id ? (prev ?? []).map((a) => (a.id === id ? data : a)) : [...(prev ?? []), data]))
    setEditing(null)
    return null
  }

  const toggle = async (a: Automation) => {
    const { data } = await api.PUT("/v1/automations/{automation_id}", {
      params: { path: { automation_id: a.id } },
      body: { ...asWrite(a), enabled: !a.enabled },
    })
    setNotice(data ? null : `Couldn't turn “${a.name}” ${a.enabled ? "off" : "on"}.`)
    if (data) setRules((prev) => (prev ?? []).map((r) => (r.id === a.id ? data : r)))
  }

  const remove = async (a: Automation) => {
    if (!window.confirm(`Delete “${a.name}”? Its runs go with it.`)) return
    const { response } = await api.DELETE("/v1/automations/{automation_id}", {
      params: { path: { automation_id: a.id } },
    })
    if (!response.ok) return setNotice(`Couldn't delete “${a.name}”.`)
    setNotice(null)
    setRules((prev) => (prev ?? []).filter((r) => r.id !== a.id))
    setRuns((prev) => prev.filter((r) => r.automation_id !== a.id))
  }

  return (
    <div className="automations-page">
      <PageHead title="Automations" meta="Rules that fill in fields on each new moment.">
        {!readOnly && editing === null && (
          <button className="btn primary slim" onClick={() => setEditing("new")}>
            New automation
          </button>
        )}
      </PageHead>

      {notice && <div className="banner error">{notice}</div>}
      {editing === "new" && (
        <Editor initial={EMPTY} onSave={(body) => save(null, body)} onCancel={() => setEditing(null)} />
      )}

      {failed && !rules ? (
        <div className="banner error">
          Couldn't load your automations.{" "}
          <button className="link-btn" onClick={() => void load()}>
            Try again
          </button>
        </div>
      ) : !rules ? (
        <div className="skeleton" style={{ height: 120 }} />
      ) : rules.length === 0 && editing !== "new" ? (
        <div className="empty">
          No automations yet. An automation checks each new moment and, when it matches, fills in
          fields you choose. It only runs on moments made after you create it.
        </div>
      ) : (
        <ul className="auto-list">
          {rules.map((a) =>
            editing === a.id ? (
              <li key={a.id}>
                <Editor initial={toDraft(a)} onSave={(body) => save(a.id, body)} onCancel={() => setEditing(null)} />
              </li>
            ) : (
              <li key={a.id} className={`auto-rule ${a.enabled ? "" : "off"}`}>
                <span className="auto-rule-text">
                  <span className="auto-rule-name">{a.name}</span>
                  <span className="row-dim">{describe(a)}</span>
                </span>
                {!readOnly && (
                  <span className="auto-rule-actions">
                    <label className="auto-check" title={a.enabled ? "Turn off" : "Turn on"}>
                      <input type="checkbox" checked={a.enabled} onChange={() => void toggle(a)} />
                      {a.enabled ? "On" : "Off"}
                    </label>
                    <button className="btn ghost slim" disabled={editing !== null} onClick={() => setEditing(a.id)}>
                      Edit
                    </button>
                    <button className="btn ghost slim" disabled={editing !== null} onClick={() => void remove(a)}>
                      Delete
                    </button>
                  </span>
                )}
              </li>
            ),
          )}
        </ul>
      )}

      {runs.length > 0 && (
        <section className="auto-runs">
          <h2 className="section-label">Recent matches</h2>
          <ul>
            {runs.map((run) => (
              <RunRow key={run.id} run={run} />
            ))}
          </ul>
        </section>
      )}
    </div>
  )
}
