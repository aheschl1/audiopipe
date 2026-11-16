import { useEffect, useRef, useState } from "react"
import { api, type SpeakerRead } from "../api/client"
import { shortId } from "../format"
import { useReadOnly } from "../readOnly"
import { categoryColor, localLabelTail } from "../speakers"

// A voice, wearable anywhere: colour dot + best-known name. Clicking opens
// the labeling popover — rename the cluster in place, or declare "this is …"
// an existing named speaker (merging the whole cluster into it, or — when
// the chip knows the voice-prints behind it via `memberIds` — moving just
// this session's voice, including out into a brand-new speaker). Voices the
// clustering tier hasn't resolved yet get an explainer instead of controls.
export default function SpeakerChip({
  clusterId,
  name,
  localLabel,
  memberIds,
  onChanged,
}: {
  clusterId: string | null | undefined
  name: string | null | undefined
  localLabel?: string | null
  // Voice-print artifact ids this chip stands for (one per diarization
  // block); enables the "just this voice" reassign actions.
  memberIds?: string[]
  // Absent: a static, read-only chip (the public home page demo).
  onChanged?: () => void
}) {
  const readOnly = useReadOnly()
  const [open, setOpen] = useState(false)
  const root = useRef<HTMLSpanElement | null>(null)

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

  const colorKey = clusterId ?? localLabel ?? "?"
  const text =
    name ??
    (localLabel ? localLabelTail(localLabel) : clusterId ? shortId(clusterId) : "voice")
  const resolved = clusterId != null && name != null

  if (!onChanged || readOnly)
    return (
      <span className="speaker-chip-root">
        <span className={`speaker-chip static ${resolved ? "" : "unresolved"}`}>
          <span className="speaker-dot" style={{ background: categoryColor(colorKey) }} />
          {text}
        </span>
      </span>
    )

  return (
    <span className="filter-menu speaker-chip-root" ref={root}>
      <button
        className={`speaker-chip ${resolved ? "" : "unresolved"}`}
        title={resolved ? `${text}. Click to relabel.` : "Click to label this voice"}
        onClick={(event) => {
          event.stopPropagation()
          setOpen((o) => !o)
        }}
      >
        <span
          className="speaker-dot"
          style={{ background: categoryColor(colorKey) }}
        />
        {text}
      </button>
      {open && (
        <LabelPop
          clusterId={clusterId ?? null}
          currentName={name ?? null}
          memberIds={memberIds ?? []}
          onDone={(changed) => {
            setOpen(false)
            if (changed) onChanged()
          }}
        />
      )}
    </span>
  )
}

// The confirm step: fold into an existing speaker (whole cluster or just
// this session's voice), or split this voice out into a brand-new speaker.
type Confirm = { kind: "existing"; target: SpeakerRead } | { kind: "new" }

function LabelPop({
  clusterId,
  currentName,
  memberIds,
  onDone,
}: {
  clusterId: string | null
  currentName: string | null
  memberIds: string[]
  onDone: (changed: boolean) => void
}) {
  const [draft, setDraft] = useState(currentName ?? "")
  const [candidates, setCandidates] = useState<SpeakerRead[] | null>(null)
  const [confirm, setConfirm] = useState<Confirm | null>(null)
  const [ejectName, setEjectName] = useState("")
  const [busy, setBusy] = useState(false)

  // "This is …" candidates: named clusters in the same embedding space.
  useEffect(() => {
    if (!clusterId) return
    let cancelled = false
    void (async () => {
      const me = await api.GET("/v1/speakers/{speaker_id}", {
        params: { path: { speaker_id: clusterId } },
      })
      const all = await api.GET("/v1/speakers", { params: { query: { limit: 200 } } })
      if (cancelled) return
      const model = me.data?.model
      setCandidates(
        (all.data?.items ?? []).filter(
          (s) => s.id !== clusterId && s.name != null && (!model || s.model === model),
        ),
      )
    })()
    return () => {
      cancelled = true
    }
  }, [clusterId])

  if (!clusterId) {
    return (
      <div className="filter-pop chip-pop">
        <div className="chip-pop-hint">
          This voice hasn't been matched to a speaker cluster yet — labels attach to
          clusters, so there's nothing to name here until the clustering tier catches
          up.
        </div>
      </div>
    )
  }

  const save = async () => {
    const name = draft.trim() || null
    if (name === currentName) {
      onDone(false)
      return
    }
    setBusy(true)
    const { error } = await api.POST("/v1/speakers/{speaker_id}/label", {
      params: { path: { speaker_id: clusterId } },
      body: { name },
    })
    setBusy(false)
    onDone(!error)
  }

  const mergeInto = async (target: SpeakerRead) => {
    setBusy(true)
    const { error } = await api.POST("/v1/speakers/{speaker_id}/merge", {
      params: { path: { speaker_id: clusterId } },
      body: { into_speaker_id: target.id },
    })
    setBusy(false)
    onDone(!error)
  }

  // Move just this session's voice-print(s) — into an existing cluster, or
  // (null target) out into a fresh one. Ejecting several prints chains them:
  // the first move creates the new cluster, the rest follow it in.
  const moveMembers = async (targetId: string | null, name: string | null) => {
    setBusy(true)
    let target = targetId
    let moved = 0
    for (const artifactId of memberIds) {
      const { data } = await api.POST(
        "/v1/speakers/{speaker_id}/members/{artifact_id}/reassign",
        {
          params: { path: { speaker_id: clusterId, artifact_id: artifactId } },
          body: { into_speaker_id: target, name: target === null ? name : null },
        },
      )
      if (!data) break
      target = data.target.id
      moved += 1
    }
    setBusy(false)
    onDone(moved > 0)
  }

  const canMove = memberIds.length > 0

  if (confirm?.kind === "existing") {
    const target = confirm.target
    return (
      <div className="filter-pop chip-pop" onClick={(e) => e.stopPropagation()}>
        <div className="merge-confirm">
          <div>
            This is <strong>{target.name}</strong> —
          </div>
          <div className="merge-actions">
            <button
              className="btn primary slim"
              disabled={busy}
              onClick={() => void mergeInto(target)}
            >
              {busy ? "Working…" : "Everywhere"}
            </button>
            {canMove && (
              <button
                className="btn slim"
                disabled={busy}
                onClick={() => void moveMembers(target.id, null)}
              >
                Just this voice
              </button>
            )}
            <button className="btn ghost slim" disabled={busy} onClick={() => setConfirm(null)}>
              Back
            </button>
          </div>
          <div className="chip-pop-hint">
            “Everywhere” merges the whole cluster into {target.name}.
            {canMove &&
              " “Just this voice” moves only this session's voice-print(s) and pins them there."}
          </div>
        </div>
      </div>
    )
  }

  if (confirm?.kind === "new") {
    return (
      <div className="filter-pop chip-pop" onClick={(e) => e.stopPropagation()}>
        <div className="merge-confirm">
          <div>
            Split this voice out of{" "}
            <strong>{currentName ?? "its cluster"}</strong> into a fresh speaker.
          </div>
          <form
            className="chip-pop-name"
            onSubmit={(e) => {
              e.preventDefault()
              void moveMembers(null, ejectName.trim() || null)
            }}
          >
            <input
              className="input slim"
              placeholder="Name the new speaker…"
              value={ejectName}
              autoFocus
              maxLength={200}
              onChange={(e) => setEjectName(e.target.value)}
            />
            <button className="btn primary slim" disabled={busy}>
              {busy ? "Moving…" : "Split"}
            </button>
          </form>
          <div className="merge-actions">
            <button className="btn ghost slim" disabled={busy} onClick={() => setConfirm(null)}>
              Back
            </button>
          </div>
          <div className="chip-pop-hint">
            Give it a name. Unnamed voices can merge back together the next time
            voices are regrouped.
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="filter-pop chip-pop" onClick={(e) => e.stopPropagation()}>
      <form
        className="chip-pop-name"
        onSubmit={(e) => {
          e.preventDefault()
          void save()
        }}
      >
        <input
          className="input slim"
          placeholder="Name this voice…"
          value={draft}
          autoFocus
          onChange={(e) => setDraft(e.target.value)}
        />
        <button className="btn primary slim" disabled={busy}>
          Save
        </button>
      </form>
      {candidates === null ? (
        <div className="loading">Loading speakers…</div>
      ) : candidates.length > 0 || canMove ? (
        <>
          <div className="chip-pop-hint">Or, this is:</div>
          {candidates.map((candidate) => (
            <button
              key={candidate.id}
              className="merge-candidate"
              disabled={busy}
              onClick={() => setConfirm({ kind: "existing", target: candidate })}
            >
              <span
                className="speaker-dot"
                style={{ background: categoryColor(candidate.id) }}
              />
              <span className="merge-candidate-main">
                <span>{candidate.name}</span>
              </span>
            </button>
          ))}
          {canMove && (
            <button
              className="merge-candidate"
              disabled={busy}
              onClick={() => setConfirm({ kind: "new" })}
            >
              <span className="speaker-dot new" />
              <span className="merge-candidate-main">
                <span>Someone new…</span>
              </span>
            </button>
          )}
        </>
      ) : null}
    </div>
  )
}
