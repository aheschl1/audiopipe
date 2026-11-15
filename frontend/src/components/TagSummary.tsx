import { useEffect, useState } from "react"
import { api, type AudioTagLabel } from "../api/client"

// Session-level tags from the audio-tag-map artifact's metadata. The map is
// the tier's whole-session summary; its metadata shape is documented in
// docs/processing-pipelines.md but untyped in OpenAPI (free-form JSONB).
export default function TagSummary({ sessionId }: { sessionId: string }) {
  const [tags, setTags] = useState<AudioTagLabel[]>([])

  useEffect(() => {
    void api
      .GET("/v1/sessions/{session_id}/artifacts", {
        params: {
          path: { session_id: sessionId },
          query: { pipeline: "audio-tag", kind: "audio-tag-map", limit: 1 },
        },
      })
      .then(({ data }) => {
        const metadata = data?.items[0]?.metadata as
          | { tags?: AudioTagLabel[] }
          | undefined
        setTags(metadata?.tags ?? [])
      })
  }, [sessionId])

  if (tags.length === 0) return null
  return (
    <section className="aside-section">
      <h2 className="section-label">Sounds</h2>
      <div className="chips tag-summary">
        {tags.map((tag) => (
          <span key={tag.label} className="chip strong" title={`score ${tag.score.toFixed(2)}`}>
            {tag.label}
          </span>
        ))}
      </div>
    </section>
  )
}
