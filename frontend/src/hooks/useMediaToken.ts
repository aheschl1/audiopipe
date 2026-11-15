import { useCallback, useEffect, useRef, useState } from "react"
import { api } from "../api/client"

// Re-minting more often than this on errors would loop on a genuinely
// broken segment rather than an expired token.
const MIN_REMINT_MS = 30_000

/** A short-lived playback token for `<img>`/`<video>` src URLs, re-minted on demand. */
export function useMediaToken(sessionId: string): {
  token: string | null
  refresh: () => void
} {
  const [token, setToken] = useState<string | null>(null)
  const mintedAt = useRef(0)

  const mint = useCallback(async () => {
    mintedAt.current = Date.now()
    const { data } = await api.POST("/v1/sessions/{session_id}/playback", {
      params: { path: { session_id: sessionId } },
    })
    if (data) setToken(data.token)
  }, [sessionId])

  useEffect(() => {
    setToken(null)
    void mint()
  }, [mint])

  const refresh = useCallback(() => {
    if (Date.now() - mintedAt.current >= MIN_REMINT_MS) void mint()
  }, [mint])

  return { token, refresh }
}
