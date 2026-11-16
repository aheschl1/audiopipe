// Image/video helpers, mirroring location.ts.

import type { ImageEvent, TimelineEvent, VideoClipEvent } from "./api/client"

export type VisualEvent = ImageEvent | VideoClipEvent

export function visualEvents(events: TimelineEvent[]): VisualEvent[] {
  return events.filter(
    (e): e is VisualEvent => e.type === "image" || e.type === "video-clip",
  )
}

export function imageEvents(events: TimelineEvent[]): ImageEvent[] {
  return events.filter((e): e is ImageEvent => e.type === "image")
}

export function mediaUrl(segmentId: string, token: string): string {
  return `/v1/segments/${segmentId}/media?token=${encodeURIComponent(token)}`
}

export function artifactUrl(artifactId: string, token: string): string {
  return `/v1/artifacts/${artifactId}/media?token=${encodeURIComponent(token)}`
}

/** The small upright JPEG when the thumbnail pipeline has run, else the original;
 * nothing for a HEIC original, which browsers can't show. */
export function imageThumbUrl(image: ImageEvent, token: string): string | undefined {
  if (image.thumbnail_id) return artifactUrl(image.thumbnail_id, token)
  return image.content_type === "image/heic" ? undefined : mediaUrl(image.segment_id, token)
}

/** Browsers can't show HEIC; fall back to the thumbnail for those. */
export function imageFullUrl(image: ImageEvent, token: string): string {
  return image.content_type === "image/heic" && image.thumbnail_id
    ? artifactUrl(image.thumbnail_id, token)
    : mediaUrl(image.segment_id, token)
}

/** The H.264 proxy when ready; the original (maybe undecodable) until then. */
export function clipUrl(clip: VideoClipEvent, token: string): string {
  return clip.proxy_id ? artifactUrl(clip.proxy_id, token) : mediaUrl(clip.segment_id, token)
}

export function fmtWallTime(iso: string): string {
  return new Date(iso).toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  })
}
