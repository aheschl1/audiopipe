import { useState } from "react"
import { type AutomationWrite, type Draft, toWrite } from "../automations"

/** The rule form: conditions, then the fields to fill. */
export default function Editor({
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
            <input className="input" placeholder="Foo, Bar" value={d.people} onChange={(e) => set("people", e.target.value)} />
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
