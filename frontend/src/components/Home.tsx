import { useState } from "react"
import { Link } from "react-router"
import { api } from "../api/client"
import { DEMO_EMAIL, DEMO_PASSWORD } from "../demo"
import HomeDemo from "./HomeDemo"

/** Public landing page, shown at / to signed-out visitors. */
export default function Home({ onAuthed }: { onAuthed: () => void }) {
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState(false)

  const tryDemo = async () => {
    setBusy(true)
    setFailed(false)
    const { data } = await api.POST("/v1/auth/session", {
      body: { email: DEMO_EMAIL, password: DEMO_PASSWORD },
    })
    setBusy(false)
    if (data) onAuthed()
    else setFailed(true)
  }

  return (
    <div className="home">
      <header className="home-bar">
        <img className="brand-lockup" src="/lockup.svg" alt="Context" />
        <Link to="/login" className="btn ghost">
          Sign in
        </Link>
      </header>
      <main className="home-hero">
        <div>
          <h1 className="home-title">Context</h1>
          <p className="home-tagline">Context for everyday, built for smart glasses</p>
          <div className="home-actions">
            <button className="btn primary home-cta" disabled={busy} onClick={() => void tryDemo()}>
              {busy ? "Opening…" : "Try the demo"}
            </button>
            <Link to="/login" className="btn home-cta">
              Sign in
            </Link>
          </div>
          {failed && <p className="home-error">The demo is unavailable right now.</p>}
        </div>
        <HomeDemo />
      </main>
    </div>
  )
}
