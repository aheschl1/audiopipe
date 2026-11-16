import { expire } from "./auth"
import type { Schemas } from "./client"

export type MomentCover = Schemas["MomentCover"]
export type MomentRead = Schemas["MomentRead"]
export type MomentItem = Schemas["MomentItem"]
export type MomentItemKind = MomentItem["kind"]
export type MomentLink = Schemas["MomentLink"]
export type MomentRelation = MomentLink["relation"]
export type MomentDetail = Schemas["MomentDetail"]
export type MomentPage = Schemas["MomentListResponse"]

/** `data` on success, else the HTTP status (0 for a network failure). */
type Result<T> = { data: T; status: number } | { data: null; status: number }

async function getJson<T>(path: string): Promise<Result<T>> {
  let response: Response
  try {
    response = await fetch(path)
  } catch {
    return { data: null, status: 0 }
  }
  if (response.status === 401) expire()
  if (!response.ok) return { data: null, status: response.status }
  return { data: (await response.json()) as T, status: response.status }
}

export function listMoments(before: string | null, limit = 20): Promise<Result<MomentPage>> {
  const query = new URLSearchParams({ limit: String(limit) })
  if (before) query.set("before", before)
  return getJson<MomentPage>(`/v1/moments?${query}`)
}

export function getMoment(id: string): Promise<Result<MomentDetail>> {
  return getJson<MomentDetail>(`/v1/moments/${encodeURIComponent(id)}`)
}

export type MomentSpeaker = MomentRead["speakers"][number]

/** The moment containing an instant of a recording; 404 when none does. */
export function momentAt(sessionId: string, atMs: number): Promise<Result<MomentRead>> {
  const query = new URLSearchParams({ session_id: sessionId, at_ms: String(Math.round(atMs)) })
  return getJson<MomentRead>(`/v1/moments/at?${query}`)
}
