import { useCallback, useContext, useEffect, useRef, useState } from "react"
import { useSearchParam } from "../useParam"
import {
  api,
  type ClusterParamsRead,
  type SimilarSpeakerRead,
  type SpeakerMemberRead,
  type SpeakerRead,
  type SpeakerTranscriptRead,
} from "../api/client"
import { fmtClock, fmtDate, shortId } from "../format"
import { categoryColor, categoryTint, initials, speakerLabel } from "../speakers"
import ClipButton, { ClipProgress } from "./ClipButton"
import Menu from "./Menu"
import { ReadOnlyContext } from "../readOnly"
import PageHead from "./PageHead"

// Centroids of one voice spread up to ~0.6; different voices sit near ~0.9.
// At or past this, the confirm step warns that the merge looks cross-voice.
const FAR_DISTANCE = 0.85
const MEMBER_PAGE = 50

// Named clusters float to the top; sort() is stable, so each half keeps the
// server's created-at order.
function sortSpeakers(items: SpeakerRead[]): SpeakerRead[] {
  return [...items].sort((a, b) => Number(b.name != null) - Number(a.name != null))
}

function Avatar({ id, name, small }: { id: string; name?: string | null; small?: boolean }) {
  return (
    <span
      className={`avatar ${small ? "small" : ""}`}
      style={{ background: categoryTint(id, 14), color: categoryColor(id) }}
    >
      {initials(name, id)}
    </span>
  )
}

// The clustering settings panel: one distance knob + the talk gate, saved
// per user (null = inherit the server default), with the rebuild trigger.
function ClusterPanel({
  params,
  busy,
  counts,
  onSave,
  onRecluster,
  onReset,
}: {
  params: ClusterParamsRead
  busy: boolean
  counts: { clusters: number; assigned: number; pinned: number } | null
  onSave: (distance: number, minTalk: number) => void
  onRecluster: () => void
  onReset: () => void
}) {
  const [distance, setDistance] = useState(params.effective.cluster_distance)
  const [minTalk, setMinTalk] = useState(params.effective.min_talk_ms)
  const [confirming, setConfirming] = useState(false)

  useEffect(() => {
    setDistance(params.effective.cluster_distance)
    setMinTalk(params.effective.min_talk_ms)
  }, [params])

  const dirty =
    distance !== params.effective.cluster_distance ||
    minTalk !== params.effective.min_talk_ms

  return (
    <div className="cluster-panel">
      <label className="score-floor">
        Voice distance
        <input
          type="range"
          min={0.3}
          max={1.0}
          step={0.01}
          value={distance}
          onChange={(e) => setDistance(Number(e.target.value))}
        />
        <span className="score-value">{distance.toFixed(2)}</span>
        {params.overrides.cluster_distance === null && !dirty && (
          <span className="chip">Default</span>
        )}
      </label>
      <label className="score-floor">
        Min talk (ms)
        <input
          className="input slim talk-input"
          type="number"
          min={0}
          step={500}
          value={minTalk}
          onChange={(e) => setMinTalk(Math.max(0, Number(e.target.value)))}
        />
        {params.overrides.min_talk_ms === null && !dirty && (
          <span className="chip">Default</span>
        )}
      </label>
      <div className="cluster-panel-hint">
        Lower distance splits more (merging is cheap); higher folds variants of
        one voice together. Names and pinned voice-prints survive a rebuild.
      </div>
      <div className="merge-actions">
        <button
          className="btn primary slim"
          disabled={busy || !dirty}
          onClick={() => onSave(distance, minTalk)}
        >
          Save
        </button>
        {confirming ? (
          <>
            <button
              className="btn primary slim"
              disabled={busy}
              onClick={() => {
                setConfirming(false)
                if (dirty) onSave(distance, minTalk)
                onRecluster()
              }}
            >
              {busy ? "Rebuilding…" : "Confirm: rebuild all clusters"}
            </button>
            <button className="btn ghost slim" onClick={() => setConfirming(false)}>
              Cancel
            </button>
          </>
        ) : (
          <button
            className="btn ghost slim"
            disabled={busy}
            onClick={() => setConfirming(true)}
          >
            Recompute clustering
          </button>
        )}
        <button className="btn ghost slim" disabled={busy} onClick={onReset}>
          Reset to defaults
        </button>
      </div>
      {counts && (
        <div className="banner">
          Rebuilt {counts.clusters} cluster{counts.clusters === 1 ? "" : "s"} from{" "}
          {counts.assigned} voice-print{counts.assigned === 1 ? "" : "s"}
          {counts.pinned > 0 && ` (${counts.pinned} pinned)`}
        </div>
      )}
    </div>
  )
}

/** Merge candidates ranked by centroid distance, then an inline confirm. */
function MergeMenu({
  speaker,
  busy,
  onMerge,
  onClose,
}: {
  speaker: SpeakerRead
  busy: boolean
  onMerge: (survivor: SpeakerRead) => void
  onClose: () => void
}) {
  const [candidates, setCandidates] = useState<SimilarSpeakerRead[] | null>(null)
  const [target, setTarget] = useState<SimilarSpeakerRead | null>(null)
  const root = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    const onOutside = (event: MouseEvent) => {
      if (root.current && !root.current.contains(event.target as Node)) onClose()
    }
    const onEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose()
    }
    document.addEventListener("mousedown", onOutside)
    document.addEventListener("keydown", onEscape)
    return () => {
      document.removeEventListener("mousedown", onOutside)
      document.removeEventListener("keydown", onEscape)
    }
  }, [onClose])

  useEffect(() => {
    void api
      .GET("/v1/speakers/{speaker_id}/similar", { params: { path: { speaker_id: speaker.id } } })
      .then(({ data }) => data && setCandidates(data.items))
  }, [speaker.id])

  return (
    <div className="filter-menu" ref={root}>
        <div className="filter-pop merge-pop">
          {target ? (
            <div className="merge-confirm">
              <div>
                Merge {speaker.embeddings} voice-print
                {speaker.embeddings === 1 ? "" : "s"} into{" "}
                <strong>{speakerLabel(target)}</strong>?
              </div>
              {target.distance >= FAR_DISTANCE && (
                <div className="merge-warning">
                  These voices are {target.distance.toFixed(2)} apart, which usually means two
                  different people. Merging will pull the voice-print center between
                  them.
                </div>
              )}
              <div className="merge-actions">
                <button
                  className="btn primary slim"
                  disabled={busy}
                  onClick={() => {
                    onMerge(target)
                    onClose()
                  }}
                >
                  {busy ? "Merging…" : "Merge"}
                </button>
                <button className="btn ghost slim" onClick={() => setTarget(null)}>
                  Back
                </button>
              </div>
            </div>
          ) : candidates === null ? (
            <div className="loading">Measuring distances…</div>
          ) : candidates.length === 0 ? (
            <div className="empty">No other clusters to merge into.</div>
          ) : (
            candidates.map((candidate) => (
              <button
                key={candidate.id}
                className="merge-candidate"
                onClick={() => setTarget(candidate)}
              >
                <Avatar id={candidate.id} name={candidate.name} small />
                <span className="merge-candidate-main">
                  <span>
                    {candidate.name ?? (
                      <span className="row-dim" title={shortId(candidate.id)}>
                        Unnamed voice
                      </span>
                    )}
                  </span>
                  <span className="row-sub">
                    {candidate.embeddings} voice-print
                    {candidate.embeddings === 1 ? "" : "s"}
                  </span>
                </span>
                <span
                  className={`distance ${candidate.distance >= FAR_DISTANCE ? "far" : ""}`}
                >
                  {candidate.distance.toFixed(2)} apart
                </span>
              </button>
            ))
          )}
        </div>
    </div>
  )
}

// Per-member move popover: pick a same-model cluster or eject into a new one.
function MoveMenu({
  source,
  others,
  busy,
  onMove,
}: {
  source: SpeakerRead
  others: SpeakerRead[]
  busy: boolean
  onMove: (targetId: string | null, name: string | null) => void
}) {
  const [open, setOpen] = useState(false)
  const [name, setName] = useState("")
  const root = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (!open) return
    const onOutside = (event: MouseEvent) => {
      if (root.current && !root.current.contains(event.target as Node)) setOpen(false)
    }
    const onEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false)
    }
    document.addEventListener("mousedown", onOutside)
    document.addEventListener("keydown", onEscape)
    return () => {
      document.removeEventListener("mousedown", onOutside)
      document.removeEventListener("keydown", onEscape)
    }
  }, [open])

  return (
    <div className="filter-menu" ref={root}>
      <button className="btn ghost slim" onClick={() => setOpen((o) => !o)}>
        Move
      </button>
      {open && (
        <div className="filter-pop merge-pop">
          {others.map((candidate) => (
            <button
              key={candidate.id}
              className="merge-candidate"
              disabled={busy}
              onClick={() => {
                onMove(candidate.id, null)
                setOpen(false)
              }}
            >
              <Avatar id={candidate.id} name={candidate.name} small />
              <span className="merge-candidate-main">
                <span>
                  {candidate.name ?? (
                    <span className="row-dim" title={shortId(candidate.id)}>Unnamed voice</span>
                  )}
                </span>
              </span>
            </button>
          ))}
          <div className="merge-confirm">
            <input
              className="input slim"
              placeholder="New voice name…"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
            {!source.name && !name.trim() && (
              <div className="merge-warning">
                Give it a name. Unnamed voices can merge back together the next time
                voices are regrouped.
              </div>
            )}
            <div className="merge-actions">
              <button
                className="btn primary slim"
                disabled={busy}
                onClick={() => {
                  onMove(null, name.trim() || null)
                  setOpen(false)
                }}
              >
                Split into a new voice
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

type MemberCache = { items: SpeakerMemberRead[]; hasMore: boolean }

export default function SpeakersView({
  onOpen,
}: {
  onOpen: (id: string, seekMs?: number, artifactId?: string) => void
}) {
  const [speakers, setSpeakers] = useState<SpeakerRead[] | null>(null)
  // The expanded speaker and its tab live in the URL so a speaker is linkable.
  const [expandedParam, setExpandedParam] = useSearchParam("speaker", "")
  const expanded = expandedParam || null
  const [tabParam, setTabParam] = useSearchParam("tab", "transcripts")
  const expandedTab: "transcripts" | "prints" = tabParam === "prints" ? "prints" : "transcripts"
  const setExpanded = (id: string | null) => setExpandedParam(id, { tab: null })
  const setExpandedTab = (tab: "transcripts" | "prints") => setTabParam(tab)
  const [mergeFor, setMergeFor] = useState<string | null>(null)
  const closeMerge = useCallback(() => setMergeFor(null), [])
  const [editing, setEditing] = useState<string | null>(null)
  const [draft, setDraft] = useState("")
  const [transcripts, setTranscripts] = useState<Record<string, SpeakerTranscriptRead[]>>({})
  const [members, setMembers] = useState<Record<string, MemberCache>>({})
  const [busy, setBusy] = useState(false)
  const [namePrompt, setNamePrompt] = useState<{
    loserId: string
    survivorId: string
    name: string
  } | null>(null)
  const [params, setParams] = useState<ClusterParamsRead | null>(null)
  const [panelOpen, setPanelOpen] = useState(false)
  const [reclusterCounts, setReclusterCounts] = useState<{
    clusters: number
    assigned: number
    pinned: number
  } | null>(null)

  const refresh = async () => {
    const { data } = await api.GET("/v1/speakers", {
      params: { query: { limit: 100, offset: 0 } },
    })
    if (data) setSpeakers(sortSpeakers(data.items))
  }
  useEffect(() => {
    void refresh()
    void api.GET("/v1/speakers/cluster-params").then(({ data }) => {
      if (data) setParams(data)
    })
  }, [])

  const rename = async (speaker: SpeakerRead) => {
    const name = draft.trim()
    const { data } = await api.POST("/v1/speakers/{speaker_id}/label", {
      params: { path: { speaker_id: speaker.id } },
      // Explicit null clears the label; the key is required by the API.
      body: { name: name === "" ? null : name },
    })
    setEditing(null)
    if (data) {
      setSpeakers((prev) =>
        sortSpeakers((prev ?? []).map((s) => (s.id === data.id ? data : s))),
      )
      // Same name on two clusters usually means one split voice — offer to
      // heal it. The just-renamed cluster merges into the pre-existing one.
      const twin = (speakers ?? []).find(
        (s) => s.id !== data.id && s.name === data.name && s.model === data.model,
      )
      setNamePrompt(
        data.name && twin
          ? { loserId: data.id, survivorId: twin.id, name: data.name }
          : null,
      )
    }
  }

  const toggle = (speaker: SpeakerRead) =>
    setExpanded(expanded === speaker.id ? null : speaker.id)

  // Whatever is expanded (by click or by deep link) gets its data fetched.
  useEffect(() => {
    if (!expanded) return
    const id = expanded
    if (!transcripts[id]) {
      void api
        .GET("/v1/speakers/{speaker_id}/transcripts", {
          params: { path: { speaker_id: id }, query: { limit: 50, offset: 0 } },
        })
        .then(({ data }) => {
          if (data) setTranscripts((prev) => ({ ...prev, [id]: data.items }))
        })
    }
    if (expandedTab === "prints" && !members[id]) void loadMembers(id)
  }, [expanded, expandedTab, transcripts, members])

  const loadMembers = async (speakerId: string, offset = 0) => {
    const { data } = await api.GET("/v1/speakers/{speaker_id}/members", {
      params: {
        path: { speaker_id: speakerId },
        query: { limit: MEMBER_PAGE, offset },
      },
    })
    if (data)
      setMembers((prev) => ({
        ...prev,
        [speakerId]: {
          items: offset === 0 ? data.items : [...(prev[speakerId]?.items ?? []), ...data.items],
          hasMore: data.has_more,
        },
      }))
  }

  // Any mutation invalidates derived caches for the clusters it touched.
  const dropCaches = (...ids: (string | null | undefined)[]) => {
    setTranscripts((prev) => {
      const next = { ...prev }
      for (const id of ids) if (id) delete next[id]
      return next
    })
    setMembers((prev) => {
      const next = { ...prev }
      for (const id of ids) if (id) delete next[id]
      return next
    })
  }

  const merge = async (loserId: string, survivor: { id: string }) => {
    setBusy(true)
    const { data } = await api.POST("/v1/speakers/{speaker_id}/merge", {
      params: { path: { speaker_id: loserId } },
      body: { into_speaker_id: survivor.id },
    })
    setBusy(false)
    if (!data) return
    setSpeakers((prev) =>
      sortSpeakers(
        (prev ?? []).filter((s) => s.id !== loserId).map((s) => (s.id === data.id ? data : s)),
      ),
    )
    dropCaches(loserId, data.id)
    if (expanded === loserId) setExpanded(null)
    setNamePrompt((prev) => (prev && prev.loserId === loserId ? null : prev))
  }

  const move = async (
    source: SpeakerRead,
    artifactId: string,
    targetId: string | null,
    name: string | null,
  ) => {
    setBusy(true)
    const { data } = await api.POST(
      "/v1/speakers/{speaker_id}/members/{artifact_id}/reassign",
      {
        params: { path: { speaker_id: source.id, artifact_id: artifactId } },
        body: { into_speaker_id: targetId, name },
      },
    )
    setBusy(false)
    if (!data) return
    setSpeakers((prev) => {
      let next = (prev ?? []).filter((s) => (data.source ? true : s.id !== source.id))
      next = next.map((s) =>
        s.id === data.target.id ? data.target : data.source && s.id === data.source.id ? data.source : s,
      )
      if (!next.some((s) => s.id === data.target.id)) next = [...next, data.target]
      return sortSpeakers(next)
    })
    dropCaches(source.id, data.target.id)
    if (data.source) void loadMembers(source.id)
    else if (expanded === source.id) setExpanded(null)
  }

  const unpin = async (speakerId: string, artifactId: string) => {
    const { data } = await api.POST(
      "/v1/speakers/{speaker_id}/members/{artifact_id}/unpin",
      { params: { path: { speaker_id: speakerId, artifact_id: artifactId } } },
    )
    if (data)
      setMembers((prev) => {
        const cache = prev[speakerId]
        if (!cache) return prev
        return {
          ...prev,
          [speakerId]: {
            ...cache,
            items: cache.items.map((m) =>
              m.artifact_id === artifactId ? { ...m, pinned: false } : m,
            ),
          },
        }
      })
  }

  const saveParams = async (distance: number, minTalk: number) => {
    if (!params) return
    setBusy(true)
    const { data } = await api.POST("/v1/speakers/cluster-params", {
      body: {
        // Equal to the server default = no override for that field.
        cluster_distance: distance === params.defaults.cluster_distance ? null : distance,
        min_talk_ms: minTalk === params.defaults.min_talk_ms ? null : minTalk,
      },
    })
    setBusy(false)
    if (data) setParams(data)
  }

  const resetParams = async () => {
    setBusy(true)
    const { data } = await api.POST("/v1/speakers/cluster-params/reset", {})
    setBusy(false)
    if (data) setParams(data)
  }

  const recluster = async () => {
    setBusy(true)
    const { data } = await api.POST("/v1/speakers/recluster", {})
    setBusy(false)
    if (!data) return
    setReclusterCounts(data)
    dropCaches(...Object.keys(members), ...Object.keys(transcripts))
    setExpanded(null)
    void refresh()
  }

  const effectiveDistance = params?.effective.cluster_distance ?? 0.65
  const readOnly = useContext(ReadOnlyContext)

  return (
    <div className="speakers-view">
      <PageHead title="People">
        <Menu
          items={[
            ...(readOnly
              ? []
              : [
                  {
                    label: panelOpen ? "Hide clustering settings" : "Clustering settings",
                    onSelect: () => setPanelOpen((o) => !o),
                  },
                ]),
            { label: "Voice map", to: "/speakers/map" },
          ]}
        />
      </PageHead>
      {panelOpen && params && (
        <ClusterPanel
          params={params}
          busy={busy}
          counts={reclusterCounts}
          onSave={(d, t) => void saveParams(d, t)}
          onRecluster={() => void recluster()}
          onReset={() => void resetParams()}
        />
      )}
      {speakers === null ? (
        <div className="loading">Loading speakers…</div>
      ) : speakers.length === 0 ? (
        <div className="empty">No voices yet.</div>
      ) : (
        <div className="list">
          {speakers.map((speaker) => (
            <div key={speaker.id} className="speaker-card">
              <div className="speaker-row">
                <Avatar id={speaker.id} name={speaker.name} />
                {editing === speaker.id ? (
                  <form
                    className="speaker-edit"
                    onSubmit={(e) => {
                      e.preventDefault()
                      void rename(speaker)
                    }}
                  >
                    <input
                      className="input slim"
                      value={draft}
                      onChange={(e) => setDraft(e.target.value)}
                      placeholder="Name (leave empty to clear)"
                      autoFocus
                    />
                    <button className="btn primary slim">Save</button>
                    <button
                      type="button"
                      className="btn ghost slim"
                      onClick={() => setEditing(null)}
                    >
                      Cancel
                    </button>
                  </form>
                ) : (
                  <>
                    <button className="speaker-name" onClick={() => toggle(speaker)}>
                      {speaker.name ?? (
                        <span className="row-dim" title={shortId(speaker.id)}>
                          Unnamed voice
                        </span>
                      )}
                    </button>
                    <span className="row-sub">
                      {speaker.sessions} recording{speaker.sessions === 1 ? "" : "s"}
                    </span>
                    <div className="speaker-actions">
                      <Menu
                        items={[
                          {
                            label: "Rename",
                            onSelect: () => {
                              setEditing(speaker.id)
                              setDraft(speaker.name ?? "")
                            },
                          },
                          { label: "Merge…", onSelect: () => setMergeFor(speaker.id) },
                          {
                            label: "Manage voice-prints",
                            onSelect: () => setExpandedParam(speaker.id, { tab: "prints" }),
                          },
                        ]}
                      />
                      {mergeFor === speaker.id && (
                        <MergeMenu
                          speaker={speaker}
                          busy={busy}
                          onMerge={(survivor) => void merge(speaker.id, survivor)}
                          onClose={closeMerge}
                        />
                      )}
                    </div>
                  </>
                )}
              </div>
              {namePrompt?.loserId === speaker.id && (
                <div className="merge-hint">
                  <span>
                    another cluster is also named “{namePrompt.name}” — merge them?
                  </span>
                  <button
                    className="btn primary slim"
                    disabled={busy}
                    onClick={() => void merge(namePrompt.loserId, { id: namePrompt.survivorId })}
                  >
                    {busy ? "Merging…" : "Merge"}
                  </button>
                  <button className="btn ghost slim" onClick={() => setNamePrompt(null)}>
                    Dismiss
                  </button>
                </div>
              )}
              {expanded === speaker.id && (
                <>
                  {expandedTab === "prints" && (
                    <button className="crumb prints-back" onClick={() => setExpandedTab("transcripts")}>
                      ← Transcripts
                    </button>
                  )}
                  {expandedTab === "transcripts" ? (
                    <div className="speaker-transcripts">
                      {(transcripts[speaker.id] ?? []).length === 0 ? (
                        <div className="empty">No transcripts.</div>
                      ) : (
                        (transcripts[speaker.id] ?? []).map((t) => {
                          const clip = {
                            key: t.artifact_id,
                            sessionId: t.session_id,
                            startMs: t.start_ms,
                            endMs: t.end_ms,
                          }
                          return (
                            <div
                              key={t.artifact_id}
                              className="row clickable"
                              onClick={() => onOpen(t.session_id, t.start_ms, t.artifact_id)}
                              title="Open moment"
                            >
                              <ClipButton clip={clip} />
                              <span className="event-time as-span">{fmtClock(t.start_ms)}</span>
                              <span className="transcript-text">{t.text}</span>
                              <ClipProgress clip={clip} />
                            </div>
                          )
                        })
                      )}
                    </div>
                  ) : (
                    <MemberList
                      cache={members[speaker.id]}
                      speaker={speaker}
                      speakers={speakers}
                      busy={busy}
                      effectiveDistance={effectiveDistance}
                      onMore={(offset) => void loadMembers(speaker.id, offset)}
                      onUnpin={(artifactId) => void unpin(speaker.id, artifactId)}
                      onMove={(artifactId, targetId, name) =>
                        void move(speaker, artifactId, targetId, name)
                      }
                    />
                  )}
                </>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function MemberList({
  cache,
  speaker,
  speakers,
  busy,
  effectiveDistance,
  onMore,
  onUnpin,
  onMove,
}: {
  cache: MemberCache | undefined
  speaker: SpeakerRead
  speakers: SpeakerRead[]
  busy: boolean
  effectiveDistance: number
  onMore: (offset: number) => void
  onUnpin: (artifactId: string) => void
  onMove: (artifactId: string, targetId: string | null, name: string | null) => void
}) {
  return (
    <div className="speaker-transcripts">
      {cache === undefined ? (
        <div className="loading">Measuring…</div>
      ) : cache.items.length === 0 ? (
        <div className="empty">No voice-prints.</div>
      ) : (
        <>
          {cache.items.map((member) => {
            const clip =
              member.clip_start_ms != null && member.clip_end_ms != null
                ? {
                    key: member.artifact_id,
                    sessionId: member.session_id,
                    startMs: member.clip_start_ms,
                    endMs: member.clip_end_ms,
                  }
                : null
            return (
              <div key={member.artifact_id} className="row member-row">
                {clip ? (
                  <ClipButton clip={clip} />
                ) : (
                  <span className="row-dim no-clip">–</span>
                )}
                <span className="member-when">{fmtDate(member.started_at)}</span>
                <span className="row-dim member-label">{member.speaker}</span>
                {member.talk_ms != null && (
                  <span className="row-sub">{(member.talk_ms / 1000).toFixed(1)}s</span>
                )}
                {member.pinned && (
                  <button
                    className="chip as-button"
                    title="Pinned to this person. Click to release."
                    onClick={() => onUnpin(member.artifact_id)}
                  >
                    Pinned
                  </button>
                )}
                <span
                  className={`distance ${member.distance > effectiveDistance ? "far" : ""}`}
                >
                  {member.distance.toFixed(2)}
                </span>
                <MoveMenu
                  source={speaker}
                  others={speakers.filter(
                    (s) => s.id !== speaker.id && s.model === speaker.model,
                  )}
                  busy={busy}
                  onMove={(targetId, name) => onMove(member.artifact_id, targetId, name)}
                />
                {clip && <ClipProgress clip={clip} />}
              </div>
            )
          })}
          {cache.hasMore && (
            <button className="btn ghost full" onClick={() => onMore(cache.items.length)}>
              More
            </button>
          )}
        </>
      )}
    </div>
  )
}
