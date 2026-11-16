from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from database.repos.pipelines.moment import ActivityRow
from processing.config import ProcessingSettings
from processing.llm import Llm, LlmError, LlmUnavailable
from processing.moments import (
    alias_speakers,
    Activity,
    Item,
    build_manifest,
    cover,
    parse_reply,
    pick_images,
    render,
    split,
    windows,
)
from processing.pipelines.moment import closed_windows

T0 = datetime(2026, 11, 14, 18, 0, tzinfo=UTC)
S1, S2, S3 = uuid4(), uuid4(), uuid4()


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def item(kind: str, start: float, end: float | None = None, **kw) -> Item:
    return Item(uuid4(), kind, kw.pop("session", S1), at(start), None if end is None else at(end), **kw)  # type: ignore[arg-type]


def test_windows_merge_across_sessions_within_the_gap() -> None:
    out = windows(
        [
            Activity(S1, at(0), at(30)),
            Activity(S2, at(35), at(35)),  # a photo 5 min after the audio
            Activity(S3, at(60), at(61)),  # 29 min later: its own window
        ],
        gap_ms=10 * 60_000,
    )
    assert [(w.start, w.end, w.session_ids) for w in out] == [
        (at(0), at(35), frozenset({S1, S2})),
        (at(60), at(61), frozenset({S3})),
    ]


def test_windows_keep_a_contained_span_inside() -> None:
    out = windows([Activity(S1, at(0), at(90)), Activity(S2, at(10), at(11))], gap_ms=60_000)
    assert len(out) == 1 and out[0].end == at(90)


def test_split_cuts_at_the_largest_quiet_gap_and_shares_sounds() -> None:
    items = [
        item("utterance", 0, 1),
        item("utterance", 20, 21),
        item("utterance", 80, 81),  # 59 min quiet before this
        item("utterance", 100, 101),
        item("sound", 0, 110),
    ]
    groups = split(items, max_ms=90 * 60_000, split_gap_ms=2 * 60_000)
    assert [[i.start for i in g if i.kind != "sound"] for g in groups] == [
        [at(0), at(20)],
        [at(80), at(100)],
    ]
    assert all(any(i.kind == "sound" for i in g) for g in groups)


def test_split_falls_back_to_even_slices_without_a_gap() -> None:
    items = [item("utterance", m, m + 1) for m in range(0, 180)]
    groups = split(items, max_ms=60 * 60_000, split_gap_ms=5 * 60_000)
    assert len(groups) == 3
    assert sum(len(g) for g in groups) == 180


def test_split_leaves_a_short_window_whole() -> None:
    items = [item("utterance", 0, 1), item("keyframe", 30)]
    assert split(items, max_ms=60 * 60_000, split_gap_ms=60_000) == [sorted(items, key=lambda i: i.start)]


def test_pick_images_keeps_photos_then_spreads_keyframes() -> None:
    photos = [item("photo", 50), item("photo", 5)]
    frames = [item("keyframe", m) for m in range(10)]
    chosen = pick_images(photos + frames, 5)
    assert len(chosen) == 5
    assert set(photos) <= set(chosen)
    assert [i.start for i in chosen] == sorted(i.start for i in chosen)
    assert {frames[0], frames[9]} <= set(chosen)


def test_manifest_labels_and_candidates() -> None:
    u1 = item("utterance", 0, 1, text="we're by the window", speaker="Sam")
    u2 = item("utterance", 10, 11, text="the move is in March", speaker="b1:SPEAKER_01")
    k1 = item("keyframe", 10.5, offset_ms=3000)
    p1 = item("photo", 40)
    s1 = item("sound", 0, 45, text="Dishes, pots, and pans", score=0.8)
    speech = item("sound", 0, 45, text="Speech", score=0.95)
    faint = item("sound", 0, 45, text="Tick", score=0.5)
    manifest = build_manifest(
        [u1, u2, k1, p1, s1, speech, faint],
        max_images=12, max_utterances=400, max_sounds=20, slack_ms=2000, min_sound_peak=0.6,
    )
    assert manifest.labels["U2"].speaker == "voice 1"
    assert {k: v.artifact_id for k, v in manifest.labels.items()} == {
        "U1": u1.artifact_id, "U2": u2.artifact_id, "K1": k1.artifact_id,
        "P1": p1.artifact_id, "S1": s1.artifact_id,
    }
    assert manifest.images == ["K1", "P1"]
    assert manifest.candidates == [("K1", "U2")]

    text = render(manifest, at(0), at(45), UTC)
    assert 'U2  18:10:00-18:11:00  voice 1  "the move is in March"' in text
    assert "S1  18:00:00-18:45:00  Dishes, pots, and pans (peak 0.80)" in text
    assert "K1  18:10:30  video frame +3.0s" in text
    assert "K1 ~ U2" in text


def test_manifest_samples_long_transcripts() -> None:
    utterances = [item("utterance", m, m + 0.5, text=str(m)) for m in range(50)]
    manifest = build_manifest(utterances, max_images=12, max_utterances=10, max_sounds=20, slack_ms=0)
    assert manifest.shown_utterances == 10 and manifest.total_utterances == 50
    assert "(10 of 50, evenly sampled)" in render(manifest, at(0), at(50), UTC)


def test_parse_reply_keeps_only_verifiable_links() -> None:
    u1, k1, k2 = item("utterance", 0, 1), item("keyframe", 0.5), item("keyframe", 30)
    labels = {"U1": u1, "K1": k1, "K2": k2}
    body = {
        "title": "Dinner by the window",
        "summary": "Dinner.",
        "people": ["Sam", "SPEAKER_01", 3],
        "topics": ["dinner", " ", "a", "b", "c", "d", "e"],
        "visible_text": "SPECIALS",
        "links": [
            {"from": "K1", "to": "U1", "relation": "depicts", "confidence": 0.9, "why": "table"},
            {"from": "K1", "to": "U1", "relation": "depicts", "confidence": 0.4},  # duplicate, lower
            {"from": "K9", "to": "U1", "relation": "depicts"},  # unknown label
            {"from": "K1", "to": "U1", "relation": "loves"},  # unknown relation
            {"from": "K1", "to": "K1", "relation": "same_scene"},  # self
            {"from": "K2", "to": "U1", "relation": "depicts"},  # 29 min apart
            {"from": "K1", "to": "U1", "relation": "mentions", "confidence": 7},
        ],
    }
    draft = parse_reply(body, labels, link_max_ms=10 * 60_000)
    assert draft.people == ("Sam", "SPEAKER_01") and draft.visible_text == ("SPECIALS",)
    assert draft.topics == ("dinner", "a", "b", "c", "d")
    got = {(l.from_id, l.to_id, l.relation): l.confidence for l in draft.links}
    assert got == {(k1.artifact_id, u1.artifact_id, "depicts"): 0.9, (k1.artifact_id, u1.artifact_id, "mentions"): 1.0}
    assert draft.dropped_links == 5


def test_span_ignores_sounds_running_past_the_moment() -> None:
    from processing.moments import span

    group = [item("sound", 0, 120), item("utterance", 30, 31), item("photo", 50)]
    assert span(group) == (at(30), at(50))


def test_parse_reply_titles_from_the_summary() -> None:
    draft = parse_reply({"summary": "A long walk along the river at dusk with the dog."}, {}, 1)
    assert draft.title == "A long walk along the river at dusk"


def test_cover_prefers_photos_then_linked_frames() -> None:
    from processing.moments import Link

    frames = [item("keyframe", m) for m in range(6)]
    photo = item("photo", 3)
    links = [Link(frames[4].artifact_id, uuid4(), "depicts", 1, ""), Link(frames[4].artifact_id, uuid4(), "mentions", 1, "")]
    chosen = cover(frames + [photo], links, count=2)
    assert set(chosen) == {photo, frames[4]}


def _row(session: UUID, start: datetime, end: datetime, *, open: bool = False, ingested: datetime | None = None) -> ActivityRow:
    return ActivityRow(
        user_id=UUID(int=1), session_id=session, start_at=start, end_at=end,
        open=open, last_ingested_at=ingested or end,
    )


def test_closed_windows_wait_for_the_gap_settling_and_open_sessions() -> None:
    settings = ProcessingSettings(moment_gap_ms=10 * 60_000, moment_settle_seconds=120)
    now = at(100)
    rows = [
        _row(S1, at(0), at(30)),  # closed long ago
        _row(S2, at(85), at(95)),  # only 5 min since: still open
        _row(S3, at(55), at(65), ingested=at(99)),  # gap passed, but uploaded 1 min ago
    ]
    closed = closed_windows(rows, now, settings)
    assert [w.session_ids for _, w in closed] == [frozenset({S1})]

    live = [_row(S1, at(0), at(30), open=True, ingested=at(95))]
    assert closed_windows(live, now, settings) == []
    stale = [_row(S1, at(0), at(30), open=True, ingested=at(30))]
    assert len(closed_windows(stale, now, settings)) == 1


def test_aliases_are_per_session() -> None:
    # Phone and glasses each diarize; their SPEAKER_00s are different people.
    a = item("utterance", 0, 1, text="hi", speaker="SPEAKER_00", session=S1)
    b = item("utterance", 1, 2, text="hey", speaker="SPEAKER_00", session=S2)
    c = item("utterance", 2, 3, text="so", speaker="SPEAKER_00", session=S1)
    named = item("utterance", 3, 4, text="ok", speaker="Sam", session=S2)
    assert [i.speaker for i in alias_speakers([a, b, c, named])] == ["voice 1", "voice 2", "voice 1", "Sam"]


def test_render_names_the_zone() -> None:
    manifest = build_manifest([item("utterance", 0, 1, text="hi")], max_images=0,
                              max_utterances=10, max_sounds=0, slack_ms=0)
    assert render(manifest, at(0), at(1), UTC).startswith("MOMENT  Saturday 2026-11-14 18:00:00 to 18:01:00 (1m 0s, UTC)")


async def test_exhausted_quota_is_not_refunded(monkeypatch) -> None:
    import httpx

    def answer(status: int, body: str):
        transport = httpx.MockTransport(lambda request: httpx.Response(status, text=body))
        llm = Llm(ProcessingSettings(llm_api_key="k"))
        llm._client = httpx.AsyncClient(base_url="http://llm", transport=transport)
        return llm

    quota = answer(429, '{"error": {"code": "insufficient_quota"}}')
    try:
        await quota.complete_json("s", "t")
    except LlmUnavailable:
        raise AssertionError("quota exhaustion must count the attempt")
    except LlmError:
        pass
    busy = answer(429, '{"error": {"code": "rate_limit_exceeded"}}')
    try:
        await busy.complete_json("s", "t")
    except LlmUnavailable:
        pass
