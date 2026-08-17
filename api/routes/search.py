"""Audio search, two ways.

- ``/search/audio`` — **contrastive**: the query text is embedded by the
  classifier service's CLAP text encoder (same joint space as the stored
  window embeddings), then pgvector ranks every window the caller owns by
  cosine distance. Open vocabulary; relative distances, no absolute cutoff.
- ``/search/tags`` — **filtering**: no model in the loop; windows are
  matched on the tagger's stored per-class sigmoid scores (exact classes,
  calibrated, thresholdable). ``/search/tags/labels`` lists the classes
  actually heard, for the filter UI.
- ``/search/transcripts`` — **lexical**: "when was X said". Postgres FTS
  (stemming, phrases, google-ish operators) over the transcript utterances,
  falling back to trigram close-spelling matches when strict finds nothing.
  A semantic ``mode`` is planned but deliberately deferred (chunk-overlap
  design first); adding it stays non-breaking.

Together they are the retrieval primitives for question-answering over
recordings: the audio routes answer "when did I hear X", transcripts answer
"when was X said"; consumers feed the hit evidence to an LLM.
"""

from uuid import UUID

from fastapi import APIRouter, Query

from api.deps import CurrentUser, Pipe
from api.embed_query import embed_query
from api.schema.search import (
    AudioSearchRead,
    AudioSearchResponse,
    TagLabelRead,
    TagLabelsResponse,
    TagSearchRead,
    TagSearchResponse,
    TranscriptSearchRead,
    TranscriptSearchResponse,
)

router = APIRouter(prefix="/search", tags=["search"])


@router.get("/audio", summary="Find audio windows by describing their sound")
async def search_audio(
    user: CurrentUser,
    pipe: Pipe,
    q: str = Query(min_length=1, max_length=500, description="A sound description, e.g. 'keyboard typing' or 'a dog barking'."),
    session_id: UUID | None = Query(None, description="Restrict matches to one session."),
    limit: int = Query(20, ge=1, le=100),
) -> AudioSearchResponse:
    """Ranked ~10s windows from your sessions whose audio best matches the
    description, nearest first. Distances are only comparable within one
    query; there is no absolute 'good match' cutoff — treat the ranking as
    the signal and let downstream consumers (or the tag scores on each hit)
    decide relevance."""
    vector, model = await embed_query(q)
    hits = await pipe.audio_embeddings.search(
        vector, user_id=user.id, session_id=session_id, limit=limit
    )
    return AudioSearchResponse(
        query=q, model=model, items=[AudioSearchRead.from_model(hit) for hit in hits]
    )


@router.get("/tags/labels", summary="List the sound classes heard in your audio")
async def list_tag_labels(user: CurrentUser, pipe: Pipe) -> TagLabelsResponse:
    """Every class the tagger stored for your windows, most frequent first —
    the vocabulary the ``/search/tags`` filter can act on."""
    rows = await pipe.tags.labels(user_id=user.id)
    return TagLabelsResponse(labels=[TagLabelRead.from_model(row) for row in rows])


@router.get("/tags", summary="Find audio windows by sound-class score")
async def search_tags(
    user: CurrentUser,
    pipe: Pipe,
    label: str = Query(min_length=1, max_length=200, description="Class to match, case-insensitive substring — 'speech' finds 'Male speech, man speaking'."),
    min_score: float = Query(0.3, ge=0.0, le=1.0, description="Keep windows where the class scored at least this."),
    session_id: UUID | None = Query(None, description="Restrict matches to one session."),
    limit: int = Query(20, ge=1, le=100),
) -> TagSearchResponse:
    """The deterministic search: exact classes with calibrated scores, best
    first. Unlike the contrastive route, scores ARE comparable across
    queries and a threshold means the same thing everywhere."""
    hits = await pipe.tags.search(
        user_id=user.id,
        label=label,
        min_score=min_score,
        session_id=session_id,
        limit=limit,
    )
    return TagSearchResponse(
        label=label,
        min_score=min_score,
        items=[TagSearchRead.from_model(hit) for hit in hits],
    )


@router.get("/transcripts", summary="Find moments by what was said")
async def search_transcripts(
    user: CurrentUser,
    pipe: Pipe,
    q: str = Query(min_length=1, max_length=500, description="Words to find. Google-ish syntax: words AND together, `or`, `-exclude`, and \"quoted phrases\" must appear adjacent."),
    session_id: UUID | None = Query(None, description="Restrict matches to one session."),
    limit: int = Query(20, ge=1, le=100),
) -> TranscriptSearchResponse:
    """Lexical search over the transcript utterances, best rank first.

    Strict full-text first (stemmed, so 'discussing' finds 'discussed');
    when that matches nothing, trigram close-spelling matches take over —
    misheard ASR words and typos still land — flagged ``fuzzy`` so the UI
    can say so. Scores are comparable within one response only.
    """
    hits = await pipe.transcripts.search(
        user_id=user.id, q=q, session_id=session_id, limit=limit
    )
    fuzzy = not hits
    if fuzzy:
        hits = await pipe.transcripts.search_fuzzy(
            user_id=user.id, q=q, session_id=session_id, limit=limit
        )
    return TranscriptSearchResponse(
        query=q,
        fuzzy=fuzzy and bool(hits),
        items=[TranscriptSearchRead.from_model(hit, fuzzy=fuzzy) for hit in hits],
    )
