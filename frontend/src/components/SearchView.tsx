import { useEffect, useRef, useState, type FormEvent } from "react"
import { api, type AudioSearchRead, type Schemas } from "../api/client"
import { fmtClock, shortId } from "../format"
import ClipButton, { ClipProgress } from "./ClipButton"
import { useSearchParam } from "../useParam"
import PageHead from "./PageHead"

type TagSearchRead = Schemas["TagSearchRead"]
type TagLabelRead = Schemas["TagLabelRead"]
type TranscriptSearchRead = Schemas["TranscriptSearchRead"]
type SnippetSegment = Schemas["SnippetSegment"]

type Mode = "describe" | "classes" | "transcripts"

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

// Two ways to find a moment:
// - describe (contrastive): free text through CLAP's text encoder, ranked by
//   embedding distance. Open vocabulary; ranks are relative per query.
// - classes (filtering): the tagger's stored per-class sigmoid scores —
//   deterministic, thresholdable, no model in the loop.
export default function SearchView({
  onOpen,
}: {
  onOpen: (id: string, seekMs?: number) => void
}) {
  const [modeParam, setMode] = useSearchParam("mode", "describe")
  const mode: Mode =
    modeParam === "classes" || modeParam === "transcripts" ? modeParam : "describe"
  // The submitted query lives in the URL; the input box is local until submit.
  const [urlQ, setUrlQ] = useSearchParam("q", "")
  const [q, setQ] = useState(urlQ)
  const ran = useRef<string | null>(null)
  const [minScore, setMinScore] = useState(0.3)
  const [known, setKnown] = useState<TagLabelRead[]>([])
  const [results, setResults] = useState<Hit[] | null>(null)
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
    if (mode === "transcripts") {
      const { data } = await api.GET("/v1/search/transcripts", {
        params: { query: { q: query.trim(), limit: 30 } },
      })
      setBusy(false)
      if (data) {
        setResults(data.items.map(fromTranscript))
        setFuzzy(data.fuzzy)
      } else {
        setResults(null)
        setError("search failed")
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
            ? "the audio search service is offline"
            : "search failed",
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
        setError("search failed")
      }
    }
  }

  // A reload or shared link re-runs the query it carries.
  useEffect(() => {
    if (urlQ && ran.current !== `${mode}\0${urlQ}`) void run(urlQ)
  }, [mode, urlQ])

  const submit = (event: FormEvent) => {
    event.preventDefault()
    void run(q)
  }

  return (
    <div className="search-view">
      <PageHead title="Search" meta="Find a moment by what was said or what it sounded like." />
      <div className="mode-toggle">
        {(
          [
            ["describe", "describe a sound"],
            ["classes", "filter by class"],
            ["transcripts", "transcripts"],
          ] as const
        ).map(([value, title]) => (
          <button
            key={value}
            className={`mode ${mode === value ? "active" : ""}`}
            onClick={() => {
              // The query survives a mode switch; stale results don't.
              setMode(value, { q: null })
              setResults(null)
              setError(null)
            }}
          >
            {title}
          </button>
        ))}
      </div>

      <form className="search-bar" onSubmit={submit}>
        <input
          className="input"
          placeholder={
            mode === "describe"
              ? "describe a sound — “keyboard typing”, “a dog barking”, “music”…"
              : mode === "classes"
                ? "class name — “speech” matches “Male speech, man speaking”…"
                : 'words that were said — supports "quoted phrases", -exclude, or'
          }
          value={q}
          onChange={(e) => setQ(e.target.value)}
          list={mode === "classes" ? "known-labels" : undefined}
          autoFocus
        />
        {mode === "classes" && (
          <datalist id="known-labels">
            {known.map((row) => (
              <option key={row.label} value={row.label} />
            ))}
          </datalist>
        )}
        <button className="btn primary" disabled={busy || !q.trim()}>
          {busy ? "searching…" : "search"}
        </button>
      </form>

      {mode === "classes" && (
        <div className="filter-extras">
          <label className="score-floor">
            min score
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
                  title={`${row.windows} windows · best ${(row.best * 100).toFixed(0)}%`}
                  onClick={() => {
                    setQ(row.label)
                    void run(row.label)
                  }}
                >
                  {row.label}
                  <span className="chip-count">{row.windows}</span>
                </button>
              ))}
            </div>
          )}
        </div>
      )}

      {error && <div className="banner error">{error}</div>}
      {fuzzy && (
        <div className="fuzzy-note">no exact matches — showing close spellings</div>
      )}
      {results && results.length === 0 && (
        <div className="empty">
          {mode === "transcripts" ? "Nothing like that was said." : "No matching audio."}
        </div>
      )}
      {results && results.length > 0 && (
        <div className="list">
          {results.map((hit, rank) => {
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
                onClick={() => onOpen(hit.sessionId, hit.startMs)}
                title="open in session"
              >
                <ClipButton clip={clip} />
                {hit.headline === null && hit.segments === null && (
                  <span className="rank">#{rank + 1}</span>
                )}
                <div className="row-main">
                  <span className="row-title">
                    {hit.headline && <span className="match-label">{hit.headline} · </span>}
                    {fmtClock(hit.startMs)} – {fmtClock(hit.endMs)}
                    <span className="row-dim"> in {shortId(hit.sessionId)}</span>
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
                <span className="row-open">open ↗</span>
                <ClipProgress clip={clip} />
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
