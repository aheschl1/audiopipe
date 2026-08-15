import { useEffect, useRef, useState, type Ref } from "react"
import { api } from "../api/client"

// <audio src> cannot carry an Authorization header, so playback runs on a
// minutes-lived token in the URL (POST /playback), re-minted once when the
// element errors (expiry mid-listen). Seeking is the browser's native Range
// handling against the stitched-WAV route.
export default function AudioPlayer({
  sessionId,
  audioRef,
}: {
  sessionId: string
  audioRef: Ref<HTMLAudioElement | null>
}) {
  const [src, setSrc] = useState<string | null>(null)
  const [failed, setFailed] = useState(false)
  const retried = useRef(false)

  const mint = async (): Promise<boolean> => {
    const { data } = await api.POST("/v1/sessions/{session_id}/playback", {
      params: { path: { session_id: sessionId } },
    })
    if (!data) {
      setFailed(true)
      return false
    }
    setFailed(false)
    setSrc(`/v1/sessions/${sessionId}/resources/audio/media?token=${encodeURIComponent(data.token)}`)
    return true
  }

  useEffect(() => {
    retried.current = false
    setSrc(null)
    void mint()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId])

  if (failed) return <div className="player-fallback">audio unavailable</div>
  if (!src) return <div className="player-fallback">preparing audio…</div>

  return (
    <audio
      ref={audioRef}
      className="player"
      controls
      preload="metadata"
      src={src}
      onError={() => {
        // One retry covers an expired token; a second failure is real
        // (no audio yet, or the session isn't stitchable).
        if (retried.current) {
          setFailed(true)
        } else {
          retried.current = true
          void mint()
        }
      }}
    />
  )
}
