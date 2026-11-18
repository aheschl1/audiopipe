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
import { momentAt } from "./api/moments"
import AbOverview from "./components/AbOverview"
import AbVoteView from "./components/AbVoteView"
import AutomationsPage from "./components/AutomationsPage"
import AutomationView, { NewAutomation } from "./components/AutomationView"
import Gate from "./components/Gate"
import GeoMap from "./components/GeoMap"
import Home from "./components/Home"
import MomentsPage from "./components/MomentsPage"
import Menu from "./components/Menu"
import MomentView from "./components/MomentView"
import Overview from "./components/Overview"
import ReadOnlyNotice from "./components/ReadOnlyNotice"
import Tour, { markSeen, tourSeen } from "./components/Tour"
import { ReadOnlyContext } from "./readOnly"
import { DEMO_EMAIL } from "./demo"
import { preloadMapLibre } from "./map/trackMap"
import SearchView from "./components/SearchView"
import SessionList from "./components/SessionList"
import SessionView from "./components/SessionView"
import TimelinePage from "./components/TimelinePage"
import SpeakerMap from "./components/SpeakerMap"
import SpeakersView from "./components/SpeakersView"
import { useSearchParam } from "./useParam"

type NavItem = { to: string; label: string; match: (path: string) => boolean }

// The voice map, Places and A/B are off the nav: reached from moments, People and recordings.
const NAV: NavItem[] = [
  { to: "/", label: "Dashboard", match: (path) => path === "/" },
  { to: "/moments", label: "Moments", match: (path) => path.startsWith("/moments") },
  { to: "/timeline", label: "Timeline", match: (path) => path === "/timeline" },
  { to: "/automations", label: "Automations", match: (path) => path.startsWith("/automations") },
  { to: "/search", label: "Search", match: (path) => path === "/search" },
  { to: "/speakers", label: "People", match: (path) => path.startsWith("/speakers") },
]

function useNav() {
  const navigate = useNavigate()
  const openSession = (id: string, seekMs?: number) =>
    navigate(seekMs === undefined ? `/sessions/${id}` : `/sessions/${id}?t=${Math.round(seekMs)}`)
  const openAb = (id: string) => navigate(`/ab/${id}`)
  const openMoment = (id: string, replace = false) => navigate(`/moments/${id}`, { replace })
  // A search hit lands in its moment when one contains it; otherwise in the recording.
  const openAt = async (sessionId: string, seekMs?: number, artifactId?: string) => {
    if (seekMs !== undefined) {
      const { data } = await momentAt(sessionId, seekMs)
      if (data) {
        const q = new URLSearchParams({ s: sessionId, t: String(Math.round(seekMs)) })
        if (artifactId) q.set("item", artifactId)
        navigate(`/moments/${data.id}?${q}`)
        return
      }
    }
    openSession(sessionId, seekMs)
  }
  // A deep link has no app history to pop, so fall back to the list page.
  const back = (fallback: string) =>
    (window.history.state?.idx ?? 0) > 0 ? navigate(-1) : navigate(fallback)
  return { openSession, openAt, openAb, openMoment, back }
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
      onBack={() => back("/")}
      onOpenSession={openSession}
      onAb={openAb}
    />
  )
}

function AutomationRoute() {
  const { id = "" } = useParams()
  return <AutomationView key={id} id={id} />
}

function MomentRoute() {
  const { id = "" } = useParams()
  const [params] = useSearchParams()
  const { openSession, openMoment, back } = useNav()
  const t = Number(params.get("t"))
  const target = {
    item: params.get("item") ?? undefined,
    sessionId: params.get("s") ?? undefined,
    ms: params.has("t") && Number.isFinite(t) ? t : undefined,
  }
  return (
    <MomentView
      key={id}
      momentId={id}
      target={target.item || target.sessionId ? target : undefined}
      onBack={() => back("/moments")}
      // Stepping replaces history, so the breadcrumb still returns to the feed.
      onNeighbour={(next) => openMoment(next, true)}
      onOpenSession={openSession}
    />
  )
}

function AbVoteRoute() {
  const { id = "" } = useParams()
  const { back } = useNav()
  return <AbVoteView key={id} sessionId={id} onBack={() => back("/ab")} />
}

function SpeakersPage() {
  const { openAt } = useNav()
  const [sub] = useSearchParam("view", "list")
  if (sub === "map") return <Navigate to="/speakers/map" replace />
  return <SpeakersView onOpen={(id, ms, artifactId) => void openAt(id, ms, artifactId)} />
}

function VoiceMapPage() {
  const { openSession } = useNav()
  return <SpeakerMap onOpen={openSession} />
}

export default function App() {
  // Unknown until /v1/me answers: the cookie is invisible to script.
  const [authed, setAuthed] = useState<boolean | null>(null)
  const [me, setMe] = useState<MeRead | null>(null)
  // The probe got neither a user nor a 401 (API restarting, network down).
  const [unreachable, setUnreachable] = useState(false)
  const [attempt, setAttempt] = useState(0)
  const [touring, setTouring] = useState(false)
  const location = useLocation()
  const navigate = useNavigate()
  const { openSession, openAt, openAb } = useNav()

  useEffect(() => {
    setExpiryHandler(() => setAuthed(false))
    dropLegacyToken()
  }, [])

  useEffect(() => {
    if (authed === false) {
      setMe(null)
      setTouring(false)
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
          if (data.user.email === DEMO_EMAIL && !tourSeen()) setTouring(true)
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
  // Signed out: the landing page at /, the sign-in card at /login, everything else back to /.
  if (!authed) {
    if (location.pathname === "/") return <Home onAuthed={() => setAuthed(true)} />
    if (location.pathname !== "/login") return <Navigate to="/" replace />
    return (
      <Gate
        onAuthed={() => {
          if (location.pathname === "/login") navigate("/", { replace: true })
          setAuthed(true)
        }}
      />
    )
  }

  const navLink = (item: NavItem) => {
    const active = item.match(location.pathname)
    return (
      <Link
        key={item.to}
        to={item.to}
        className={`nav-item ${active ? "active" : ""}`}
        aria-current={active ? "page" : undefined}
      >
        {item.label}
      </Link>
    )
  }
  const wide =
    location.pathname === "/" ||
    location.pathname === "/map" ||
    location.pathname.startsWith("/sessions/") ||
    location.pathname.startsWith("/moments/") ||
    location.pathname === "/speakers/map"

  return (
    <div className={`app ${touring ? "touring" : ""}`}>
      {touring && (
        <div className="tour-note" role="note">
          The demo video was created with Google Veo. Pipeline output is real.
        </div>
      )}
      <nav className="sidebar" aria-label="Main">
        <Link to="/" className="brand">
          <img className="brand-lockup" src="/lockup.svg" alt="Context" />
        </Link>
        <div className="nav-items">
          {NAV.map((item) => navLink(item))}
        </div>
        <div className="nav-foot">
          <Link to="/map" className={`nav-quiet ${location.pathname === "/map" ? "active" : ""}`}>
            Map
          </Link>
          <Menu
            label="Account"
            trigger={
              <>
                <span className="whoami">{me?.user.display_name ?? me?.user.email}</span>
                <span aria-hidden="true">⋯</span>
              </>
            }
            items={[
              ...(me?.user.email === DEMO_EMAIL
                ? [
                    touring
                      ? {
                          label: "End tour",
                          onSelect: () => {
                            markSeen()
                            setTouring(false)
                          },
                        }
                      : { label: "Take the tour", onSelect: () => setTouring(true) },
                  ]
                : []),
              {
                label: me?.user.email === DEMO_EMAIL ? "Exit demo" : "Log out",
                onSelect: () => void api.DELETE("/v1/auth/session").finally(() => setAuthed(false)),
              },
            ]}
          />
        </div>
      </nav>

      <main className={`main ${wide ? "wide" : ""}`}>
        <ReadOnlyContext.Provider value={!!me?.user.read_only}>
        <Routes>
          <Route path="/" element={<Overview />} />
          <Route path="/moments" element={<MomentsPage />} />
          <Route path="/moments/:id" element={<MomentRoute />} />
          <Route path="/timeline" element={<TimelinePage />} />
          <Route path="/sessions" element={<SessionList onOpen={openSession} />} />
          <Route path="/sessions/:id" element={<SessionRoute />} />
          <Route path="/search" element={<SearchView onOpen={openAt} onOpenRecording={openSession} />} />
          <Route path="/speakers" element={<SpeakersPage />} />
          <Route path="/speakers/map" element={<VoiceMapPage />} />
          <Route path="/map" element={<GeoMap onOpen={openSession} />} />
          <Route path="/automations" element={<AutomationsPage />} />
          <Route path="/automations/new" element={<NewAutomation />} />
          <Route path="/automations/:id" element={<AutomationRoute />} />
          <Route path="/ab" element={<AbOverview onVote={openAb} />} />
          <Route path="/ab/:id" element={<AbVoteRoute />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
        </ReadOnlyContext.Provider>
      </main>
      <ReadOnlyNotice />
      {touring && <Tour onClose={() => setTouring(false)} />}
    </div>
  )
}
