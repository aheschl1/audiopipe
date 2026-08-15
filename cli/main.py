"""Administrative CLI over the database and blob store.

    uv run python -m cli.main check
    uv run python -m cli.main users create --email me@example.com
    uv run python -m cli.main tokens issue me@example.com --name laptop
    uv run python -m cli.main segments ingest SESSION_ID clip.wav
"""

import asyncio
import json
import mimetypes
from datetime import timedelta
from functools import wraps
from pathlib import Path
from typing import Any, Callable
from uuid import UUID

import click
from botocore.exceptions import BotoCoreError, ClientError

from database import DatabaseError, DatabasePipe, close_pool, get_settings
from database.exceptions import NotFoundError
from database.schema.sessions import SessionCreate
from database.schema.users import UserCreate
from services import segments as segment_service
from storage import BlobNotFoundError, BlobPipe, close_blob_client
from storage import get_settings as get_storage_settings


def async_command(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Run an ``async def`` command body, then close the pool and blob client."""

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        async def run() -> Any:
            try:
                return await fn(*args, **kwargs)
            finally:
                await close_blob_client()
                await close_pool()

        try:
            return asyncio.run(run())
        except (DatabaseError, BlobNotFoundError) as exc:
            raise click.ClickException(str(exc)) from exc
        except (ClientError, BotoCoreError) as exc:
            raise click.ClickException(f"blob store error: {exc}") from exc
        except OSError as exc:  # connection refused, DNS failure, ...
            raise click.ClickException(f"cannot reach the database: {exc}") from exc

    return wrapper


async def _require_user(pipe: DatabasePipe, email: str):
    user = await pipe.users.get_by_email(email)
    if user is None:
        raise NotFoundError("user", email)
    return user


def _fmt(value: Any) -> str:
    return "-" if value is None else str(value)


def _human_bytes(count: int) -> str:
    size = float(count)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{count} B"


@click.group()
def cli() -> None:
    """Manage users, auth tokens, recording sessions, and resource segments."""


@cli.command()
@async_command
async def check() -> None:
    """Verify that the configured database is reachable."""
    settings = get_settings()
    async with DatabasePipe() as pipe:
        await pipe.ping()
    click.echo(f"ok: connected to {settings.summary}")


#  users


@cli.group()
def users() -> None:
    """Manage user accounts."""


@users.command("create")
@click.option("--email", required=True, help="Login email (case-insensitive).")
@click.option("--display-name", default=None, help="Optional display name.")
@click.option(
    "--password",
    prompt=True,
    hide_input=True,
    confirmation_prompt=True,
    help="Prompted for if not supplied.",
)
@async_command
async def users_create(email: str, display_name: str | None, password: str) -> None:
    """Create a user."""
    async with DatabasePipe() as pipe:
        user = await pipe.users.create(
            UserCreate(email=email, password=password, display_name=display_name)
        )
    click.echo(f"created {user.email} ({user.id})")


@users.command("list")
@click.option("--limit", default=50, show_default=True)
@click.option("--offset", default=0, show_default=True)
@async_command
async def users_list(limit: int, offset: int) -> None:
    """List users."""
    async with DatabasePipe() as pipe:
        found = await pipe.users.list(limit=limit, offset=offset)
    if not found:
        click.echo("no users")
        return
    for user in found:
        state = "active" if user.is_active else "disabled"
        click.echo(f"{user.id}  {user.email:<32} {state:<9} {_fmt(user.display_name)}")


@users.command("show")
@click.argument("email")
@async_command
async def users_show(email: str) -> None:
    """Show one user, their tokens, and their stored data."""
    async with DatabasePipe() as pipe:
        user = await _require_user(pipe, email)
        tokens = await pipe.tokens.list_for_user(user.id)
        sessions = await pipe.sessions.list_for_user(user.id)
        usage = await pipe.segments.usage_for_user(user.id)
    click.echo(f"id           {user.id}")
    click.echo(f"email        {user.email}")
    click.echo(f"display name {_fmt(user.display_name)}")
    click.echo(f"active       {user.is_active}")
    click.echo(f"created      {user.created_at:%Y-%m-%d %H:%M:%S %Z}")
    click.echo(f"tokens       {sum(token.is_active for token in tokens)} active / {len(tokens)}")
    click.echo(f"sessions     {sum(s.is_open for s in sessions)} open / {len(sessions)}")
    if not usage:
        click.echo("stored       nothing")
    for row in usage:
        click.echo(
            f"{row.resource:<12} {row.segments} segments, {_human_bytes(row.total_bytes)}"
        )


@users.command("set-password")
@click.argument("email")
@click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True)
@async_command
async def users_set_password(email: str, password: str) -> None:
    """Replace a user's password."""
    async with DatabasePipe() as pipe:
        user = await _require_user(pipe, email)
        await pipe.users.set_password(user.id, password)
    click.echo(f"password updated for {user.email}")


@users.command("activate")
@click.argument("email")
@async_command
async def users_activate(email: str) -> None:
    """Re-enable a user."""
    async with DatabasePipe() as pipe:
        user = await _require_user(pipe, email)
        await pipe.users.set_active(user.id, True)
    click.echo(f"activated {user.email}")


@users.command("deactivate")
@click.argument("email")
@async_command
async def users_deactivate(email: str) -> None:
    """Disable a user without deleting them."""
    async with DatabasePipe() as pipe:
        user = await _require_user(pipe, email)
        await pipe.users.set_active(user.id, False)
    click.echo(f"deactivated {user.email}")


@users.command("delete")
@click.argument("email")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
@async_command
async def users_delete(email: str, yes: bool) -> None:
    """Delete a user and all of their tokens."""
    if not yes:
        click.confirm(f"permanently delete {email} and all their tokens?", abort=True)
    async with DatabasePipe() as pipe:
        user = await _require_user(pipe, email)
        await pipe.users.delete(user.id)
    click.echo(f"deleted {user.email}")


# -------------------------------------------------------------------------- tokens


@cli.group()
def tokens() -> None:
    """Manage API auth tokens."""


@tokens.command("issue")
@click.argument("email")
@click.option("--name", default=None, help="Label, e.g. the machine it is for.")
@click.option("--ttl-days", type=int, default=None, help="Expire after N days.")
@async_command
async def tokens_issue(email: str, name: str | None, ttl_days: int | None) -> None:
    """Mint a token for a user."""
    ttl = timedelta(days=ttl_days) if ttl_days else None
    async with DatabasePipe() as pipe:
        user = await _require_user(pipe, email)
        issued = await pipe.tokens.issue(user.id, name=name, ttl=ttl)
    click.echo(issued.token.get_secret_value())
    click.secho("store this now -- only the hash is kept, it cannot be shown again", fg="yellow")


@tokens.command("list")
@click.argument("email")
@click.option("--active-only", is_flag=True, help="Hide revoked and expired tokens.")
@async_command
async def tokens_list(email: str, active_only: bool) -> None:
    """List a user's tokens."""
    async with DatabasePipe() as pipe:
        user = await _require_user(pipe, email)
        found = await pipe.tokens.list_for_user(user.id, include_inactive=not active_only)
    if not found:
        click.echo("no tokens")
        return
    for token in found:
        if token.revoked_at is not None:
            state = "revoked"
        elif token.is_expired:
            state = "expired"
        else:
            state = "active"
        click.echo(
            f"{token.id}  {state:<8} {_fmt(token.name):<16} "
            f"created {token.created_at:%Y-%m-%d}  last used {_fmt(token.last_used_at)}"
        )


@tokens.command("revoke")
@click.argument("token_id", type=click.UUID)
@async_command
async def tokens_revoke(token_id: UUID) -> None:
    """Revoke one token by id."""
    async with DatabasePipe() as pipe:
        revoked = await pipe.tokens.revoke(token_id)
    click.echo("revoked" if revoked else "no such active token")


@tokens.command("revoke-all")
@click.argument("email")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
@async_command
async def tokens_revoke_all(email: str, yes: bool) -> None:
    """Revoke every active token belonging to a user."""
    if not yes:
        click.confirm(f"revoke all active tokens for {email}?", abort=True)
    async with DatabasePipe() as pipe:
        user = await _require_user(pipe, email)
        count = await pipe.tokens.revoke_all_for_user(user.id)
    click.echo(f"revoked {count} token(s)")


@tokens.command("purge-expired")
@async_command
async def tokens_purge_expired() -> None:
    """Delete tokens that are past their expiry."""
    async with DatabasePipe() as pipe:
        count = await pipe.tokens.purge_expired()
    click.echo(f"deleted {count} expired token(s)")


# --------------------------------------------------------------------------- blobs


@cli.group()
def blobs() -> None:
    """Inspect the blob store holding blob-backed resources (audio)."""


@blobs.command("check")
@async_command
async def blobs_check() -> None:
    """Verify the blob store is reachable, creating the bucket if needed."""
    settings = get_storage_settings()
    async with BlobPipe() as store:
        await store.ensure_bucket()
    click.echo(f"ok: bucket ready at {settings.summary}")


@blobs.command("ls")
@click.argument("prefix", default="")
@async_command
async def blobs_ls(prefix: str) -> None:
    """List object keys under a prefix."""
    async with BlobPipe() as store:
        keys = await store.keys_under(prefix)
    for key in keys:
        click.echo(key)
    click.echo(f"{len(keys)} object(s)")


# ------------------------------------------------------------------------ sessions


@cli.group()
def sessions() -> None:
    """Manage recording sessions."""


@sessions.command("start")
@click.argument("email")
@click.option("--device", default=None, help="Free-form device identifier.")
@click.option("--label", default=None, help="Human label for the session.")
@async_command
async def sessions_start(email: str, device: str | None, label: str | None) -> None:
    """Open a recording session for a user."""
    async with DatabasePipe() as pipe:
        user = await _require_user(pipe, email)
        session = await pipe.sessions.create(
            SessionCreate(user_id=user.id, device=device, label=label)
        )
    click.echo(f"{session.id}")


@sessions.command("list")
@click.argument("email")
@click.option("--open-only", is_flag=True, help="Only sessions that have not ended.")
@async_command
async def sessions_list(email: str, open_only: bool) -> None:
    """List a user's sessions."""
    async with DatabasePipe() as pipe:
        user = await _require_user(pipe, email)
        found = await pipe.sessions.list_for_user(user.id, open_only=open_only)
    if not found:
        click.echo("no sessions")
        return
    for session in found:
        state = "open" if session.is_open else "ended"
        click.echo(
            f"{session.id}  {state:<6} {session.started_at:%Y-%m-%d %H:%M}  "
            f"{_fmt(session.device):<16} {_fmt(session.label)}"
        )


@sessions.command("end")
@click.argument("session_id", type=click.UUID)
@async_command
async def sessions_end(session_id: UUID) -> None:
    """Close a session."""
    async with DatabasePipe() as pipe:
        session = await pipe.sessions.end(session_id)
    click.echo(f"ended at {session.ended_at:%Y-%m-%d %H:%M:%S %Z}")


@sessions.command("delete")
@click.argument("session_id", type=click.UUID)
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
@async_command
async def sessions_delete(session_id: UUID, yes: bool) -> None:
    """Delete a session, its segments, and their stored data."""
    if not yes:
        click.confirm(f"delete session {session_id} and all of its data?", abort=True)
    removed = await segment_service.delete_session(session_id)
    click.echo(f"deleted session and {removed} object(s)")


@sessions.command("retranscribe")
@click.argument("session_id", type=click.UUID)
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
@async_command
async def sessions_retranscribe(session_id: UUID, yes: bool) -> None:
    """Redo a session's transcripts with the current ASR backend.

    Deletes the transcribe tier's artifacts and job history in one
    transaction; the worker's discovery re-queues every utterance.
    Manual edits to transcripts are lost.
    """
    if not yes:
        click.confirm(
            f"re-transcribe session {session_id}? existing transcripts "
            "(including manual edits) are deleted",
            abort=True,
        )
    async with DatabasePipe() as pipe:
        if await pipe.sessions.get(session_id) is None:
            raise NotFoundError("session", str(session_id))
        transcripts = await pipe.artifacts.delete_for_pipeline(session_id, "transcribe")
        jobs = await pipe.jobs.delete_for_session(session_id, "transcribe")
    click.echo(
        f"deleted {transcripts} transcript(s) and {jobs} job(s); "
        "the worker will re-transcribe every utterance"
    )


@sessions.command("rediarize")
@click.argument("session_id", type=click.UUID)
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
@async_command
async def sessions_rediarize(session_id: UUID, yes: bool) -> None:
    """Redo a session's diarization (and its transcripts) with the current
    diarizer.

    Deletes the diarize and transcribe tiers' artifacts and job history in
    one transaction; the worker's discovery re-runs diarization from the
    surviving speech-map, then re-transcribes the new utterances. Pins are
    keyed on (session, label, model) and survive. Manual transcript edits
    are lost.
    """
    if not yes:
        click.confirm(
            f"re-diarize session {session_id}? existing turns, voice-prints "
            "and transcripts (including manual edits) are deleted",
            abort=True,
        )
    async with DatabasePipe() as pipe:
        if await pipe.sessions.get(session_id) is None:
            raise NotFoundError("session", str(session_id))
        counts = {}
        for pipeline in ("diarize", "transcribe", "transcribe-ab"):
            counts[pipeline] = await pipe.artifacts.delete_for_pipeline(
                session_id, pipeline
            )
            await pipe.jobs.delete_for_session(session_id, pipeline)
        await pipe.jobs.delete_for_session(session_id, "speaker-cluster")
    click.echo(
        f"deleted {counts['diarize']} diarize and {counts['transcribe']} "
        "transcribe artifact(s); the worker will re-diarize from the speech-map"
    )


# ------------------------------------------------------------------------ speakers


@cli.group()
def speakers() -> None:
    """Inspect and rebuild global speaker clusters."""


@speakers.command("list")
@click.argument("email")
@async_command
async def speakers_list(email: str) -> None:
    """List a user's speaker clusters with membership counts."""
    async with DatabasePipe() as pipe:
        user = await _require_user(pipe, email)
        found = await pipe.speakers.list_for_user(user.id, limit=200)
    if not found:
        click.echo("no speakers")
        return
    for speaker in found:
        click.echo(
            f"{speaker.id}  {_fmt(speaker.name):<20} "
            f"{speaker.embeddings:>4} embedding(s) across {speaker.sessions} session(s)"
        )


@speakers.command("recluster")
@click.argument("email")
@click.option(
    "--distance",
    type=float,
    default=None,
    help="One-off clustering threshold; defaults to the saved override or config.",
)
@click.option(
    "--min-talk-ms",
    type=int,
    default=None,
    help="One-off voice-print gate; same fallback.",
)
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
@async_command
async def speakers_recluster(
    email: str, distance: float | None, min_talk_ms: int | None, yes: bool
) -> None:
    """Rebuild a user's clusters (the same batch every session-end runs).

    Named clusters survive as identity anchors and reclaim their members;
    pinned voice-prints keep their asserted identity; unlabeled cluster ids
    churn. Safe to run any time — assignments are derived data.
    """
    from processing.config import get_settings as processing_settings
    from processing.pipelines.speaker_cluster import rebuild_user_clusters

    if not yes:
        click.confirm(
            f"rebuild all speaker clusters for {email}? (named clusters survive)",
            abort=True,
        )
    overrides: dict = {}
    if distance is not None:
        overrides["cluster_distance"] = distance
    if min_talk_ms is not None:
        overrides["cluster_min_talk_ms"] = min_talk_ms
    async with DatabasePipe() as pipe:
        user = await _require_user(pipe, email)
        result = await rebuild_user_clusters(
            pipe, user.id, processing_settings(), overrides or None
        )
    click.echo(
        f"{result['assigned']} voice-print(s) across {result['clusters']} "
        f"cluster(s); {result['pinned']} pinned, {result['skipped_short']} "
        f"skipped short"
    )


# ------------------------------------------------------------------------ segments


@cli.group()
def segments() -> None:
    """Ingest and retrieve resource segments."""


@segments.command("ingest")
@click.argument("session_id", type=click.UUID)
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--resource", default="audio", help="Resource type of the file's contents.")
@click.option("--content-type", default=None, help="Defaults to a guess from the filename.")
@click.option("--duration-ms", type=int, default=None)
@click.option("--offset-ms", type=int, default=None)
@async_command
async def segments_ingest(
    session_id: UUID,
    path: Path,
    resource: str,
    content_type: str | None,
    duration_ms: int | None,
    offset_ms: int | None,
) -> None:
    """Store a local file as the next segment of a session."""
    guessed = content_type or mimetypes.guess_type(path.name)[0]
    segment = await segment_service.ingest_segment(
        session_id,
        path.read_bytes(),
        resource=resource,
        content_type=guessed,
        filename=path.name,
        duration_ms=duration_ms,
        offset_ms=offset_ms,
    )
    click.echo(f"{segment.id}  seq {segment.sequence}  {_human_bytes(segment.byte_size)}")
    click.echo(segment.object_key if segment.object_key else "(stored inline)")


@segments.command("list")
@click.argument("session_id", type=click.UUID)
@click.option("--resource", default=None, help="Only segments of this resource.")
@async_command
async def segments_list(session_id: UUID, resource: str | None) -> None:
    """List the segments of a session, in order."""
    async with DatabasePipe() as pipe:
        found = await pipe.segments.list_for_session(session_id, resource=resource)
    if not found:
        click.echo("no segments")
        return
    for segment in found:
        click.echo(
            f"{segment.id}  seq {segment.sequence:<5} {segment.resource:<9} "
            f"{_human_bytes(segment.byte_size):>10}  {segment.content_type:<16} "
            f"{segment.ingested_at:%Y-%m-%d %H:%M}"
        )


@segments.command("show")
@click.argument("segment_id", type=click.UUID)
@async_command
async def segments_show(segment_id: UUID) -> None:
    """Show one segment's metadata."""
    async with DatabasePipe() as pipe:
        segment = await pipe.segments.get(segment_id)
    if segment is None:
        raise NotFoundError("segment", segment_id)
    click.echo(f"id         {segment.id}")
    click.echo(f"session    {segment.session_id}")
    click.echo(f"resource   {segment.resource}")
    click.echo(f"sequence   {segment.sequence}")
    click.echo(f"ingested   {segment.ingested_at:%Y-%m-%d %H:%M:%S %Z}")
    click.echo(f"size       {_human_bytes(segment.byte_size)} ({segment.byte_size} bytes)")
    click.echo(f"type       {segment.content_type}")
    click.echo(f"sha256     {_fmt(segment.checksum_hex)}")
    if segment.attrs:
        click.echo(f"attrs      {segment.attrs}")
    if segment.object_key is not None:
        click.echo(f"object     {segment.bucket}/{segment.object_key}")
    else:
        click.echo("stored     inline")


@segments.command("download")
@click.argument("segment_id", type=click.UUID)
@click.argument("dest", type=click.Path(dir_okay=False, path_type=Path))
@async_command
async def segments_download(segment_id: UUID, dest: Path) -> None:
    """Write a segment's stored bytes (or inline payload as JSON) to a file."""
    try:
        segment, data = await segment_service.read_segment(segment_id)
        source = segment.object_key
    except segment_service.NoBlobError:
        async with DatabasePipe() as pipe:
            segment = await pipe.segments.get(segment_id)
        assert segment is not None  # NoBlobError implies the row exists
        data = json.dumps(segment.payload).encode()
        source = "inline payload"
    dest.write_bytes(data)
    click.echo(f"wrote {_human_bytes(len(data))} to {dest} (from {source})")


@segments.command("url")
@click.argument("segment_id", type=click.UUID)
@click.option("--expires-in", type=int, default=None, help="Seconds; defaults to config.")
@async_command
async def segments_url(segment_id: UUID, expires_in: int | None) -> None:
    """Print a presigned URL that serves the segment directly."""
    try:
        click.echo(await segment_service.segment_url(segment_id, expires_in=expires_in))
    except segment_service.NoBlobError:
        raise click.ClickException(
            "segment stores its payload inline; use 'segments download'"
        ) from None


@segments.command("delete")
@click.argument("segment_id", type=click.UUID)
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
@async_command
async def segments_delete(segment_id: UUID, yes: bool) -> None:
    """Delete a segment and its stored data."""
    if not yes:
        click.confirm(f"delete segment {segment_id} and its stored data?", abort=True)
    deleted = await segment_service.delete_segment(segment_id)
    click.echo("deleted" if deleted else "no such segment")


if __name__ == "__main__":
    cli()
