// The public demo account is read-only: the UI stays whole, the API refuses its writes,
// and each refusal raises a small notice instead of an error.
export const READ_ONLY_DETAIL = "this account is read-only"

const listeners = new Set<() => void>()

export function onRefused(fn: () => void): () => void {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

export function refused(): void {
  for (const fn of listeners) fn()
}

/** True when an API error body is the read-only refusal (already announced by the notice). */
export function isRefusal(error: unknown): boolean {
  return (error as { detail?: unknown } | undefined)?.detail === READ_ONLY_DETAIL
}
