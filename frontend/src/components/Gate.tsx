import { useState, type FormEvent } from "react"
import { api } from "../api/client"

// Full-screen login card; the app renders nothing else until authed.
// Accounts are created out-of-band (`cli users create`) — no signup here.
export default function Gate({ onAuthed }: { onAuthed: () => void }) {
  const [email, setEmail] = useState("")
  const [password, setPassword] = useState("")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setBusy(true)
    setError(null)
    const { data, error: err } = await api.POST("/v1/auth/session", { body: { email, password } })
    setBusy(false)
    if (data) {
      onAuthed()
    } else {
      setError(typeof err?.detail === "string" ? err.detail : "Sign-in failed. Check your email and password.")
    }
  }

  return (
    <div className="gate">
      <form className="gate-card" onSubmit={submit}>
        <h1 className="gate-title">
          <img className="brand-lockup" src="/lockup.svg" alt="Context" />
        </h1>
        <div className="gate-subtitle">What you saw and heard, indexed</div>
        <input
          className="input"
          type="email"
          placeholder="Email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          autoFocus
          required
        />
        <input
          className="input"
          type="password"
          placeholder="Password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          required
        />
        {error && <div className="gate-error">{error}</div>}
        <button className="btn primary full" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  )
}
