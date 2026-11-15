// Sign-in lives in an HttpOnly cookie (POST /v1/auth/session) the browser
// sends on every request, media elements included; script never sees it. A
// 401 anywhere routes through the expiry handler so App can flip to the Gate.

// Cookie-authenticated writes must carry this header (the API's CSRF check).
export const CSRF_HEADER = "X-Requested-With"
export const CSRF_VALUE = "context"

let onExpire: (() => void) | null = null

export function setExpiryHandler(fn: () => void): void {
  onExpire = fn
}

export function expire(): void {
  onExpire?.()
}

// Bearer tokens from before the cookie switch; never read again.
export function dropLegacyToken(): void {
  try {
    localStorage.removeItem("audiopipeline.token")
  } catch {
    // storage blocked: nothing to drop
  }
}
