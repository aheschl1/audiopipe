// The one API client. Every request and response type comes from the
// generated schema (npm run gen ← frontend/openapi.json ← make openapi);
// no model is ever hand-written here.

import createClient, { type Middleware } from "openapi-fetch"
import type { components, paths } from "./schema"
import { CSRF_HEADER, CSRF_VALUE, expire } from "./auth"

export type Schemas = components["schemas"]
export type SessionRead = Schemas["SessionRead"]
export type SegmentRead = Schemas["SegmentRead"]
export type TimelineEvent = Schemas["TimelineEvent"]
export type TranscriptEvent = Schemas["TranscriptEvent"]
export type AudioTagEvent = Schemas["AudioTagEvent"]
export type AudioTagLabel = Schemas["AudioTagLabel"]
export type SoundSpanEvent = Schemas["SoundSpanEvent"]
export type ConversationEvent = Schemas["ConversationEvent"]
export type ConversationRead = Schemas["ConversationRead"]
export type ConversationDetail = Schemas["ConversationDetail"]
export type LocationPointEvent = Schemas["LocationPointEvent"]
export type LocationPointRead = Schemas["LocationPointRead"]
export type ImageEvent = Schemas["ImageEvent"]
export type VideoClipEvent = Schemas["VideoClipEvent"]
export type SessionTrackRead = Schemas["SessionTrackRead"]
export type TrackPointRead = Schemas["TrackPointRead"]
export type ArtifactRead = Schemas["ArtifactRead"]
export type SpeakerRead = Schemas["SpeakerRead"]
export type SimilarSpeakerRead = Schemas["SimilarSpeakerRead"]
export type SpeakerMemberRead = Schemas["SpeakerMemberRead"]
export type ClusterParamsRead = Schemas["ClusterParamsRead"]
export type SpeakerTranscriptRead = Schemas["SpeakerTranscriptRead"]
export type SessionSpeakerRead = Schemas["SessionSpeakerRead"]
export type SpeakerProjectionRead = Schemas["SpeakerProjectionRead"]
export type ProjectionPointRead = Schemas["ProjectionPointRead"]
export type ProjectionClusterRead = Schemas["ProjectionClusterRead"]
export type AudioSearchRead = Schemas["AudioSearchRead"]
export type MeRead = Schemas["MeRead"]
export type AbSessionRead = Schemas["AbSessionRead"]
export type AbUtteranceRead = Schemas["AbUtteranceRead"]
export type AbResultsRead = Schemas["AbResultsRead"]

const auth: Middleware = {
  onRequest({ request }) {
    request.headers.set(CSRF_HEADER, CSRF_VALUE)
    return request
  },
  onResponse({ request, response }) {
    // A failed login is not an expired session; only established auth expires.
    if (response.status === 401 && !request.url.includes("/v1/auth/")) expire()
    return response
  },
}

export const api = createClient<paths>({ baseUrl: "" })
api.use(auth)
