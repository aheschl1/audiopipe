import { useEffect, useRef, useState, type FormEvent } from "react"
import { api, type AudioSearchRead, type Schemas } from "../api/client"
import { clock, dayLabel, fmtClock } from "../format"
import ClipButton, { ClipProgress } from "./ClipButton"
import { useSearchParam } from "../useParam"
import FrameResults from "./FrameResults"
import PageHead from "./PageHead"

type TagSearchRead = Schemas["TagSearchRead"]
type TagLabelRead = Schemas["TagLabelRead"]
type TranscriptSearchRead = Schemas["TranscriptSearchRead"]
type SnippetSegment = Schemas["SnippetSegment"]
type FrameSearchRead = Schemas["FrameSearchRead"]

type Mode = "describe" | "classes" | "transcripts" | "frames"

// One row shape serves every search mode.
type Hit = {
  artifactId: string
  sessionId: string
  startMs: number
  endMs: number
  headline: string | null // matched class + score (filter mode)
  labels: { label: string; score: number }[]
  segments: SnippetSegment[] | null // highlighted snippet (transcript mode)
  speaker: string | null
}

function fromContrastive(hit: AudioSearchRead): Hit {
  return {
    artifactId: hit.artifact_id,
    sessionId: hit.session_id,
    startMs: hit.start_ms,
    endMs: hit.end_ms,
    headline: null,
    labels: hit.labels,
    segments: null,
    speaker: null,
  }
}

function fromTag(hit: TagSearchRead): Hit {
  return {
    artifactId: hit.artifact_id,
    sessionId: hit.session_id,
    startMs: hit.start_ms,
    endMs: hit.end_ms,
    headline: `${hit.label} · ${(hit.score * 100).toFixed(0)}%`,
    labels: hit.labels.filter((tag) => tag.label !== hit.label),
    segments: null,
    speaker: null,
  }
}

function fromTranscript(hit: TranscriptSearchRead): Hit {
  return {
    artifactId: hit.artifact_id,
    sessionId: hit.session_id,
    startMs: hit.start_ms,
    endMs: hit.end_ms,
    headline: null,
    labels: [],
    segments: hit.segments,
    speaker: hit.speaker ?? null,
  }
}

const starts = new Map<string, string | null>()

/** Session start times for the given ids, fetched once each, so hits can show a wall-clock time. */
function useSessionStarts(ids: string[]): (sessionId: string, ms: number | null | undefined) => string | null {
  const [, bump] = useState(0)
  const key = [...new Set(ids)].sort().join(",")
  useEffect(() => {
    const missing = key.split(",").filter((id) => id && !starts.has(id))
    if (!missing.length) return
    let live = true
    for (const id of missing) starts.set(id, null)
    void Promise.all(
      missing.map((id) =>
        api
          .GET("/v1/sessions/{session_id}", { params: { path: { session_id: id } } })
          .then(({ data }) => data && starts.set(id, data.started_at))
          .catch(() => undefined),
      ),
    ).then(() => live && bump((n) => n + 1))
    return () => {
      live = false
    }
  }, [key])
  return (sessionId, ms) => {
    if (ms === null || ms === undefined) return null
    const start = starts.get(sessionId)
    if (!start) return fmtClock(ms)
    const at = new Date(Date.parse(start) + ms).toISOString()
    return `${dayLabel(at)} ${clock(at)}`
  }
}

const TABS = [
  ["transcripts", "Said"],
  ["frames", "Seen"],
  ["describe", "Sounds"],
] as const

// Ways to find a moment:
// - describe (contrastive): free text through CLAP's text encoder, ranked by
//   embedding distance. Open vocabulary; ranks are relative per query.
// - classes (filtering): the tagger's stored per-class sigmoid scores —
//   deterministic, thresholdable, no model in the loop.
// - frames (contrastive, visual): free text through SigLIP against keyframes.
export default function SearchView({
  onOpen,
  onOpenRecording,
}: {
  onOpen: (id: string, seekMs?: number, artifactId?: string) => void
  onOpenRecording: (id: string, seekMs?: number) => void
}) {
  const [modeParam, setMode] = useSearchParam("mode", "transcripts")
  const mode: Mode =
    modeParam === "classes" || modeParam === "describe" || modeParam === "frames"
      ? modeParam
      : "transcripts"
  // The submitted query lives in the URL; the input box is local until submit.
  const [urlQ, setUrlQ] = useSearchParam("q", "")
  const [q, setQ] = useState(urlQ)
  const ran = useRef<string | null>(null)
  const [minScore, setMinScore] = useState(0.3)
  const [known, setKnown] = useState<TagLabelRead[]>([])
  const [results, setResults] = useState<Hit[] | null>(null)
  const [frames, setFrames] = useState<FrameSearchRead[] | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [fuzzy, setFuzzy] = useState(false)

  // The class vocabulary actually heard in this user's audio, for chips +
  // datalist suggestions in filter mode.
  useEffect(() => {
    void api.GET("/v1/search/tags/labels").then(({ data }) => {
      if (data) setKnown(data.labels)
    })
  }, [])

  const run = async (query: string) => {
    if (!query.trim()) return
    ran.current = `${mode}\0${query.trim()}`
    setUrlQ(query.trim())
    setBusy(true)
    setError(null)
    setFuzzy(false)
    if (mode === "frames") {
      const { data, response } = await api.GET("/v1/search/frames", {
        params: { query: { q: query.trim(), limit: 30 } },
      })
      setBusy(false)
      setFrames(data ? data.items : null)
      if (!data)
        setError(response.status === 502 ? "Image search is offline. Try again later." : "Search failed. Try again.")
    } else if (mode === "transcripts") {
      const { data } = await api.GET("/v1/search/transcripts", {
        params: { query: { q: query.trim(), limit: 30 } },
      })
      setBusy(false)
      if (data) {
        setResults(data.items.map(fromTranscript))
        setFuzzy(data.fuzzy)
      } else {
        setResults(null)
        setError("Search failed. Try again.")
      }
    } else if (mode === "describe") {
      const { data, response } = await api.GET("/v1/search/audio", {
        params: { query: { q: query.trim(), limit: 30 } },
      })
      setBusy(false)
      if (data) setResults(data.items.map(fromContrastive))
      else {
        setResults(null)
        setError(
          response.status === 502
            ? "Sound search is offline. Try again later."
            : "Search failed. Try again.",
        )
      }
    } else {
      const { data } = await api.GET("/v1/search/tags", {
        params: { query: { label: query.trim(), min_score: minScore, limit: 30 } },
      })
      setBusy(false)
      if (data) setResults(data.items.map(fromTag))
      else {
        setResults(null)
        setError("Search failed. Try again.")
      }
    }
  }

  // A reload or shared link re-runs the query it carries.
  useEffect(() => {
    if (urlQ && ran.current !== `${mode}\0${urlQ}`) void run(urlQ)
  }, [mode, urlQ])

  // The query survives a mode switch; stale results don't.
  const switchMode = (value: Mode) => {
    setMode(value, { q: null })
    setResults(null)
    setFrames(null)
    setError(null)
  }

  const stamp = useSessionStarts([
    ...(results ?? []).map((h) => h.sessionId),
    ...(frames ?? []).map((h) => h.session_id),
  ])

  const submit = (event: FormEvent) => {
    event.preventDefault()
    void run(q)
  }

  return (
    <div className="search-view">
      <PageHead title="Search" />
      <div className="search-modes">
        <div className="mode-toggle">
          {TABS.map(([value, title]) => {
            const active = mode === value || (value === "describe" && mode === "classes")
            return (
              <button
                key={value}
                className={`mode ${active ? "active" : ""}`}
                aria-pressed={active}
                onClick={() => !active && switchMode(value)}
              >
                {title}
              </button>
            )
          })}
        </div>
        {(mode === "describe" || mode === "classes") && (
          <label className="exact-class">
            <input
              type="checkbox"
              checked={mode === "classes"}
              onChange={(e) => switchMode(e.target.checked ? "classes" : "describe")}
            />
            Exact class
          </label>
        )}
      </div>

      <form className="search-bar" onSubmit={submit}>
        <input
          className="input"
          placeholder={
            mode === "describe"
              ? "Describe a sound…"
              : mode === "classes"
                ? "Class name…"
                : mode === "frames"
                  ? "Describe a scene…"
                  : "What was said…"
          }
          value={q}
          onChange={(e) => setQ(e.target.value)}
          list={mode === "classes" ? "known-labels" : undefined}
          autoFocus
        />
        {mode === "transcripts" && (
          <span className="search-help" title={'Use "quoted phrases", -exclude, and OR between words.'} aria-label="Search syntax">
            ?
          </span>
        )}
        {mode === "classes" && (
          <datalist id="known-labels">
            {known.map((row) => (
              <option key={row.label} value={row.label} />
            ))}
          </datalist>
        )}
        <button className="btn primary" disabled={busy || !q.trim()}>
          {busy ? "Searching…" : "Search"}
        </button>
      </form>

      {mode === "classes" && (
        <div className="filter-extras">
          <label className="score-floor">
            Min score
            <input
              type="range"
              min={0}
              max={0.95}
              step={0.05}
              value={minScore}
              onChange={(e) => setMinScore(Number(e.target.value))}
            />
            <span className="score-value">{minScore.toFixed(2)}</span>
          </label>
          {known.length > 0 && (
            <div className="chips label-cloud">
              {known.slice(0, 14).map((row) => (
                <button
                  key={row.label}
                  className="chip as-button"
                  onClick={() => {
                    setQ(row.label)
                    void run(row.label)
                  }}
                >
                  {row.label}
                </button>
              ))}
            </div>
          )}
        </div>
      )}

      {error && <div className="banner error">{error}</div>}
      {fuzzy && (
        <div className="fuzzy-note">No exact matches. Showing close spellings.</div>
      )}
      {frames && frames.length === 0 && <div className="empty">No matching photos or video.</div>}
      {frames && frames.length > 0 && <FrameResults hits={frames} onOpen={onOpen} stamp={stamp} />}
      {results && results.length === 0 && (
        <div className="empty">
          {mode === "transcripts" ? "Nothing like that was said." : "No matching audio."}
        </div>
      )}
      {results && results.length > 0 && (
        <div className="list">
          {results.map((hit) => {
            const clip = {
              key: hit.artifactId,
              sessionId: hit.sessionId,
              startMs: hit.startMs,
              endMs: hit.endMs,
            }
            return (
              <div
                key={hit.artifactId}
                className="row clickable"
                onClick={() => onOpen(hit.sessionId, hit.startMs, hit.artifactId)}
                title="Open the moment"
              >
                <ClipButton clip={clip} />
                <div className="row-main">
                  <span className="row-title">
                    {hit.headline && <span className="match-label">{hit.headline} · </span>}
                    {stamp(hit.sessionId, hit.startMs)}
                    {hit.speaker && <span className="row-dim"> · {hit.speaker}</span>}
                  </span>
                  {hit.segments && (
                    <span className="snippet">
                      {hit.segments.map((segment, i) =>
                        segment.match ? (
                          <mark key={i}>{segment.text}</mark>
                        ) : (
                          <span key={i}>{segment.text}</span>
                        ),
                      )}
                    </span>
                  )}
                  {hit.labels.length > 0 && (
                    <span className="chips">
                      {hit.labels.slice(0, 4).map((tag) => (
                        <span key={tag.label} className="chip">
                          {tag.label}
                        </span>
                      ))}
                    </span>
                  )}
                </div>
                <button
                  className="btn ghost slim row-alt icon-only"
                  title="Open full recording"
                  aria-label="Open full recording"
                  onClick={(e) => {
                    e.stopPropagation()
                    onOpenRecording(hit.sessionId, hit.startMs)
                  }}
                >
                  ↗
                </button>
                <ClipProgress clip={clip} />
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
