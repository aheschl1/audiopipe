import { api, type Schemas } from "./api/client"
import { clock, dayLabel } from "./format"
import { isRefusal } from "./readOnly"

export type Automation = Schemas["AutomationRead"]
export type AutomationWrite = Schemas["AutomationWrite"]
export type AutomationRun = Schemas["AutomationRunRead"]

/** The editor's flat form of a rule; lists are comma- or line-separated text. */
export type Draft = {
  name: string
  enabled: boolean
  people: string
  topics: string
  keywords: string
  minMinutes: string
  ask: string
  fields: { name: string; description: string }[]
}

export const EMPTY: Draft = {
  name: "",
  enabled: true,
  people: "",
  topics: "",
  keywords: "",
  minMinutes: "",
  ask: "",
  fields: [{ name: "", description: "" }],
}

const list = (text: string) =>
  text
    .split(/[,\n]/)
    .map((s) => s.trim())
    .filter(Boolean)

export function toDraft(a: Automation): Draft {
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

export function toWrite(d: Draft): AutomationWrite {
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
export function asWrite(a: Automation): AutomationWrite {
  const { name, enabled, trigger, conditions, actions } = a
  return { name, enabled, trigger, conditions, actions }
}

type Condition = { kind: string; value: string }

/** The rule's conditions as kind/value pairs, in the order they're checked. */
export function conditions(a: Automation): Condition[] {
  const c = a.conditions
  return [
    ...(c.people.length ? [{ kind: "People", value: c.people.join(", ") }] : []),
    ...(c.topics.length ? [{ kind: "Topics", value: c.topics.join(", ") }] : []),
    ...(c.keywords.length ? [{ kind: "Words", value: c.keywords.join(", ") }] : []),
    ...(c.min_minutes ? [{ kind: "Length", value: `≥ ${c.min_minutes} min` }] : []),
    ...(c.ask ? [{ kind: "Model", value: c.ask }] : []),
  ]
}

export function fieldNames(a: Automation): string[] {
  return a.actions.flatMap((x) => x.fields.map((f) => f.name))
}

/** Display names for the API's trigger and action kinds. */
export const TRIGGER = { moment: "New moment" } as const
export const ACTION = { extract: "Extract fields" } as const

/** FastAPI's 422 detail as one readable line; null for a read-only refusal, which the notice covers. */
export function problem(error: unknown): string | null {
  if (isRefusal(error)) return null
  const detail = (error as { detail?: unknown } | undefined)?.detail
  if (typeof detail === "string") return detail
  if (Array.isArray(detail) && detail.length) {
    const first = detail[0] as { loc?: unknown[]; msg?: string }
    const where = (first.loc ?? []).filter((p) => p !== "body").join(" › ")
    return where ? `${where}: ${first.msg}` : (first.msg ?? "Invalid automation.")
  }
  return "Couldn't save the automation."
}

/** Flip a rule on or off without touching the rest of it. */
export function setEnabled(a: Automation, enabled: boolean) {
  return api.PUT("/v1/automations/{automation_id}", {
    params: { path: { automation_id: a.id } },
    body: { ...asWrite(a), enabled },
  })
}

/** "Today 9:00 AM", for a run or a match. */
export function when(iso: string): string {
  return `${dayLabel(iso)} ${clock(iso)}`
}
