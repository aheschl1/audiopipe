import { useEffect, useState } from "react"
import {
  Link,
  Navigate,
  Route,
  Routes,
  useLocation,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router"
import { dropLegacyToken, setExpiryHandler } from "./api/auth"
import { api, type MeRead } from "./api/client"
import AbOverview from "./components/AbOverview"
import AbVoteView from "./components/AbVoteView"
import Gate from "./components/Gate"
import GeoMap from "./components/GeoMap"
import { preloadMapLibre } from "./map/trackMap"
import SearchView from "./components/SearchView"
import SessionList from "./components/SessionList"
import SessionView from "./components/SessionView"
import SpeakerMap from "./components/SpeakerMap"
import SpeakersView from "./components/SpeakersView"
import { useSearchParam } from "./useParam"

const NAV = [
  { to: "/sessions", label: "Sessions", match: (path: string) => path.startsWith("/sessions") },
  { to: "/search", label: "Search", match: (path: string) => path === "/search" },
  {
    to: "/speakers",
    label: "Speakers",
    match: (path: string, view: string | null) => path === "/speakers" && view !== "map",
  },
  {
    to: "/speakers?view=map",
    label: "Voice map",
    match: (path: string, view: string | null) => path === "/speakers" && view === "map",
  },
  { to: "/map", label: "Places", match: (path: string) => path === "/map" },
  { to: "/ab", label: "A/B", match: (path: string) => path.startsWith("/ab") },
] as const

function useNav() {
  const navigate = useNavigate()
  const openSession = (id: string, seekMs?: number) =>
    navigate(seekMs === undefined ? `/sessions/${id}` : `/sessions/${id}?t=${Math.round(seekMs)}`)
  const openAb = (id: string) => navigate(`/ab/${id}`)
  // A deep link has no app history to pop, so fall back to the list page.
  const back = (fallback: string) =>
    (window.history.state?.idx ?? 0) > 0 ? navigate(-1) : navigate(fallback)
  return { openSession, openAb, back }
}

function SessionRoute() {
  const { id = "" } = useParams()
  const [params] = useSearchParams()
  const { openSession, openAb, back } = useNav()
  const t = Number(params.get("t"))
  return (
    <SessionView
      key={id}
      sessionId={id}
      seekMs={params.has("t") && Number.isFinite(t) ? t : undefined}
      onBack={() => back("/sessions")}
      onOpenSession={openSession}
      onAb={openAb}
    />
  )
}

function AbVoteRoute() {
  const { id = "" } = useParams()
  const { back } = useNav()
  return <AbVoteView key={id} sessionId={id} onBack={() => back("/ab")} />
}

function SpeakersPage() {
  const { openSession } = useNav()
  const [sub] = useSearchParam("view", "list")
  return sub === "map" ? <SpeakerMap onOpen={openSession} /> : <SpeakersView onOpen={openSession} />
}

export default function App() {
  // Unknown until /v1/me answers: the cookie is invisible to script.
  const [authed, setAuthed] = useState<boolean | null>(null)
  const [me, setMe] = useState<MeRead | null>(null)
  // The probe got neither a user nor a 401 (API restarting, network down).
  const [unreachable, setUnreachable] = useState(false)
  const [attempt, setAttempt] = useState(0)
  const location = useLocation()
  const { openSession, openAb } = useNav()

  useEffect(() => {
    setExpiryHandler(() => setAuthed(false))
    dropLegacyToken()
  }, [])

  useEffect(() => {
    if (authed === false) {
      setMe(null)
      return
    }
    if (authed && me) return
    setUnreachable(false)
    api
      .GET("/v1/me")
      .then(({ data, response }) => {
        if (data) {
          setMe(data)
          setAuthed(true)
        } else if (response.status === 401) {
          setAuthed(false)
        } else {
          setUnreachable(true)
        }
      })
      .catch(() => setUnreachable(true))
  }, [authed, me, attempt])

  // Warm the maplibre chunk (~1.5 MB with its worker) once the app is idle,
  // so the first map open doesn't stack the download on top of style, tiles,
  // and track data.
  useEffect(() => {
    if (!authed) return
    const timer = window.setTimeout(() => preloadMapLibre(), 3000)
    return () => window.clearTimeout(timer)
  }, [authed])

  if (authed === null) {
    if (!unreachable) return null
    return (
      <div className="gate">
        <div className="gate-card">
          <div className="gate-subtitle">Can't reach the server.</div>
          <button className="btn primary full" onClick={() => setAttempt((n) => n + 1)}>
            Try again
          </button>
        </div>
      </div>
    )
  }
  if (!authed) return <Gate onAuthed={() => setAuthed(true)} />

  const view = new URLSearchParams(location.search).get("view")
  const wide =
    location.pathname === "/map" ||
    location.pathname.startsWith("/sessions/") ||
    (location.pathname === "/speakers" && view === "map")

  return (
    <div className="app">
      <nav className="sidebar" aria-label="main">
        <Link to="/sessions" className="brand">
          <img className="brand-mark" src="/logo.svg" alt="" />
          context
        </Link>
        <div className="nav-items">
          {NAV.map((item) => (
            <Link
              key={item.to}
              to={item.to}
              className={`nav-item ${item.match(location.pathname, view) ? "active" : ""}`}
              aria-current={item.match(location.pathname, view) ? "page" : undefined}
            >
              {item.label}
            </Link>
          ))}
        </div>
        <div className="nav-foot">
          {me && <span className="whoami">{me.user.display_name ?? me.user.email}</span>}
          <button
            className="btn ghost slim"
            onClick={() => {
              void api.DELETE("/v1/auth/session").finally(() => setAuthed(false))
            }}
          >
            log out
          </button>
        </div>
      </nav>

      <main className={`main ${wide ? "wide" : ""}`}>
        <Routes>
          <Route path="/sessions" element={<SessionList onOpen={openSession} />} />
          <Route path="/sessions/:id" element={<SessionRoute />} />
          <Route path="/search" element={<SearchView onOpen={openSession} />} />
          <Route path="/speakers" element={<SpeakersPage />} />
          <Route path="/map" element={<GeoMap onOpen={openSession} />} />
          <Route path="/ab" element={<AbOverview onVote={openAb} />} />
          <Route path="/ab/:id" element={<AbVoteRoute />} />
          <Route path="*" element={<Navigate to="/sessions" replace />} />
        </Routes>
      </main>
    </div>
  )
}
