import { useState, type FormEvent } from "react"
import { setToken } from "../api/auth"
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
    const { data, error: err } = await api.POST("/v1/auth/login", {
      body: { email, password, name: "web" },
    })
    setBusy(false)
    if (data) {
      setToken(data.token)
      onAuthed()
    } else {
      setError(typeof err?.detail === "string" ? err.detail : "login failed")
    }
  }

  return (
    <div className="gate">
      <form className="gate-card" onSubmit={submit}>
        <h1 className="gate-title">
          <img className="brand-mark" src="/logo.svg" alt="" />
          context
        </h1>
        <div className="gate-subtitle">what you saw and heard, indexed</div>
        <input
          className="input"
          type="email"
          placeholder="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          autoFocus
          required
        />
        <input
          className="input"
          type="password"
          placeholder="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          required
        />
        {error && <div className="gate-error">{error}</div>}
        <button className="btn primary full" disabled={busy}>
          {busy ? "signing in…" : "sign in"}
        </button>
      </form>
    </div>
  )
}
