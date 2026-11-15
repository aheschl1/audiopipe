// One app-wide clip player: search results and speaker transcripts play a
// [startMs, endMs) slice of a session's audio in place, without navigating.
// A single hidden <audio> element is reused, authenticated by the sign-in
// cookie. Seeking into the stitched WAV rides the browser's native Range
// requests, so only the played slice crosses the wire.

import { useSyncExternalStore } from "react"
export type Clip = {
  key: string
  sessionId: string
  startMs: number
  endMs: number
}

export type ClipPlayerState = {
  key: string | null
  status: "idle" | "loading" | "playing"
  progressMs: number
}

const IDLE: ClipPlayerState = { key: null, status: "idle", progressMs: 0 }

let state: ClipPlayerState = IDLE
let audio: HTMLAudioElement | null = null
let current: Clip | null = null
let loadedSrc = ""
const listeners = new Set<() => void>()

function emit(next: ClipPlayerState): void {
  state = next
  listeners.forEach((listener) => listener())
}

function ensureAudio(): HTMLAudioElement {
  if (audio) return audio
  audio = new Audio()
  audio.preload = "auto"
  audio.addEventListener("timeupdate", () => {
    if (!current || !audio) return
    const ms = audio.currentTime * 1000
    if (ms >= current.endMs) {
      stopClip()
      return
    }
    emit({ ...state, progressMs: Math.max(0, ms - current.startMs) })
  })
  audio.addEventListener("ended", stopClip)
  audio.addEventListener("error", () => {
    loadedSrc = ""
    stopClip()
  })
  return audio
}

export function stopClip(): void {
  audio?.pause()
  current = null
  emit(IDLE)
}

export async function toggleClip(clip: Clip): Promise<void> {
  if (state.key === clip.key) {
    stopClip()
    return
  }
  const element = ensureAudio()
  element.pause()
  current = clip
  emit({ key: clip.key, status: "loading", progressMs: 0 })

  const src = `/v1/sessions/${clip.sessionId}/resources/audio/media`
  if (loadedSrc !== src) {
    element.src = src
    loadedSrc = src
  }
  if (element.readyState === 0) {
    const ok = await new Promise<boolean>((resolve) => {
      const done = (result: boolean) => {
        element.removeEventListener("loadedmetadata", onLoaded)
        element.removeEventListener("error", onError)
        resolve(result)
      }
      const onLoaded = () => done(true)
      const onError = () => done(false)
      element.addEventListener("loadedmetadata", onLoaded)
      element.addEventListener("error", onError)
    })
    if (current?.key !== clip.key) return
    if (!ok) {
      stopClip()
      return
    }
  }

  element.currentTime = clip.startMs / 1000
  try {
    await element.play()
    if (current?.key === clip.key) {
      emit({ key: clip.key, status: "playing", progressMs: 0 })
    }
  } catch {
    if (current?.key === clip.key) stopClip()
  }
}

export function useClipPlayer(): ClipPlayerState {
  return useSyncExternalStore(
    (callback) => {
      listeners.add(callback)
      return () => listeners.delete(callback)
    },
    () => state,
  )
}
