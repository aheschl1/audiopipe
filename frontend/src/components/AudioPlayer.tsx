import { useEffect, useState, type Ref } from "react"

// The sign-in cookie authenticates the element's own requests; seeking is the
// browser's native Range handling against the stitched-WAV route.
export default function AudioPlayer({
  sessionId,
  audioRef,
}: {
  sessionId: string
  audioRef: Ref<HTMLAudioElement | null>
}) {
  const [failed, setFailed] = useState(false)
  const src = `/v1/sessions/${sessionId}/resources/audio/media`

  useEffect(() => setFailed(false), [sessionId])

  if (failed) return <div className="player-fallback">audio unavailable</div>

  return (
    <audio
      ref={audioRef}
      className="player"
      controls
      preload="metadata"
      src={src}
      // No audio yet, or the session isn't stitchable.
      onError={() => setFailed(true)}
    />
  )
}
