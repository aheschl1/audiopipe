"""End-to-end MCP: the /mcp mount, per-call bearer auth, and the
cross-session query tools over seeded rows."""

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from database.pipe import DatabasePipe
from database.schema.artifacts import ArtifactCreate
from database.schema.embeddings import AudioEmbeddingCreate
from database.schema.sessions import SessionCreate
from tests.e2e.conftest import Account, make_account

STARTED_AT = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)

TOOLS = {
    "list_sessions", "get_session", "get_transcript", "get_timeline",
    "day_summary", "search_transcripts", "search_audio", "search_sound_tags",
    "list_sound_labels", "list_speakers", "get_speaker_transcripts",
}


@asynccontextmanager
async def _mcp(server: str, token: str | None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with streamable_http_client(
        f"{server}/mcp", http_client=create_mcp_http_client(headers=headers)
    ) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


def _payload(result) -> dict:
    assert not result.is_error, result.content
    if getattr(result, "structured_content", None):
        return result.structured_content
    return json.loads(result.content[0].text)


async def _seed(account: Account):
    """One ended session with a transcript utterance, a tagged window and
    its CLAP embedding (4-dim, matching the stub's text embedding)."""
    async with DatabasePipe() as pipe:
        session = await pipe.sessions.create(
            SessionCreate(
                user_id=account.user.id, device="glasses-01",
                label="morning walk", started_at=STARTED_AT,
            )
        )
        await pipe.sessions.end(session.id, ended_at=STARTED_AT + timedelta(hours=1))
        await pipe.artifacts.create(
            ArtifactCreate(
                pipeline="transcribe", kind="transcript", session_id=session.id,
                start_ms=60_000, end_ms=63_000,
                metadata={
                    "text": "the quick brown fox", "chars": 19,
                    "model": "stub", "speaker": "b1:SPEAKER_00",
                },
            )
        )
        window = await pipe.artifacts.create(
            ArtifactCreate(
                pipeline="audio-tag", kind="audio-tag", session_id=session.id,
                start_ms=60_000, end_ms=70_000,
                metadata={"labels": [{"label": "Music", "score": 0.8}], "model": "stub"},
            )
        )
        await pipe.audio_embeddings.create_many(
            [
                AudioEmbeddingCreate(
                    artifact_id=window.id, session_id=session.id,
                    model="stub-clap", embedding=(0.9, 0.1, 0.0, 0.0),
                )
            ]
        )
    return session


async def test_lists_tools_and_rejects_missing_token(server: str) -> None:
    account = await make_account()
    async with _mcp(server, account.token) as session:
        tools = await session.list_tools()
        assert TOOLS <= {tool.name for tool in tools.tools}
    async with _mcp(server, None) as session:
        result = await session.call_tool(
            "list_sessions",
            {"start": "2026-08-10T00:00:00Z", "end": "2026-08-11T00:00:00Z"},
        )
        assert result.is_error
        assert "Bearer" in result.content[0].text


async def test_list_sessions_overlap_semantics(server: str) -> None:
    account = await make_account()
    seeded = await _seed(account)
    async with DatabasePipe() as pipe:
        open_session = await pipe.sessions.create(
            SessionCreate(
                user_id=account.user.id, device="glasses-01",
                started_at=STARTED_AT + timedelta(hours=2),
            )
        )
    async with _mcp(server, account.token) as session:
        # A window inside the seeded session catches it and the open one.
        both = _payload(
            await session.call_tool(
                "list_sessions",
                {"start": "2026-08-10T08:30:00Z", "end": "2026-08-11T00:00:00Z"},
            )
        )
        ids = [row["session_id"] for row in both["sessions"]]
        assert ids == [str(seeded.id), str(open_session.id)]
        assert both["sessions"][1]["ended_at"] is None
        # A window entirely before the seeded session excludes everything.
        none = _payload(
            await session.call_tool(
                "list_sessions",
                {"start": "2026-08-09T00:00:00Z", "end": "2026-08-10T08:00:00Z"},
            )
        )
        assert none["sessions"] == []


async def test_transcript_and_searches_carry_absolute_time(server: str) -> None:
    account = await make_account()
    seeded = await _seed(account)
    expected = STARTED_AT + timedelta(milliseconds=60_000)
    async with _mcp(server, account.token) as session:
        transcript = _payload(
            await session.call_tool("get_transcript", {"session_id": str(seeded.id)})
        )
        assert transcript["text"] == "[08:01:00] b1:SPEAKER_00: the quick brown fox"
        assert transcript["next_offset"] is None

        found = _payload(
            await session.call_tool("search_transcripts", {"query": "fox"})
        )
        assert found["fuzzy"] is False
        hit = found["hits"][0]
        assert datetime.fromisoformat(hit["time"]) == expected
        assert hit["session_label"] == "morning walk"

        # Wall-clock bounds exclude the utterance when the window misses it.
        empty = _payload(
            await session.call_tool(
                "search_transcripts",
                {"query": "fox", "end": "2026-08-10T08:00:30Z"},
            )
        )
        assert empty["hits"] == []

        tags = _payload(
            await session.call_tool("search_sound_tags", {"label": "mus"})
        )
        assert tags["hits"][0]["label"] == "Music"
        assert datetime.fromisoformat(tags["hits"][0]["time"]) == expected

        audio = _payload(await session.call_tool("search_audio", {"query": "music"}))
        assert audio["model"] == "stub-clap"
        assert [hit["session_id"] for hit in audio["hits"]] == [str(seeded.id)]

        summary = _payload(
            await session.call_tool(
                "day_summary",
                {"start": "2026-08-10T00:00:00Z", "end": "2026-08-11T00:00:00Z"},
            )
        )
        assert [row["label"] for row in summary["top_sounds"]] == ["Music"]

        timeline = _payload(
            await session.call_tool("get_timeline", {"session_id": str(seeded.id)})
        )
        types = [event["type"] for event in timeline["events"]]
        assert types == ["session-start", "transcript", "audio-tag", "session-end"]


async def test_other_users_session_is_not_found(server: str) -> None:
    account = await make_account()
    intruder = await make_account()
    seeded = await _seed(account)
    async with _mcp(server, intruder.token) as session:
        result = await session.call_tool(
            "get_session", {"session_id": str(seeded.id)}
        )
        assert result.is_error
        assert "not found" in result.content[0].text
