"""resource segments: audio becomes one resource type among many

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-14

Sessions now carry generic *resources* — streams of typed data — of which
audio is merely the first. The ingest table is renamed accordingly and
generalized:

* ``resource`` tags each row with its type ('audio', 'location', ...). The
  default is permanent, not transitional: it matches the wire default of a
  chunk header that omits the field.
* Per-resource parameters move into ``attrs`` JSONB (validated in code by the
  resource registry) — the codec/sample_rate_hz/channels columns were the
  audio-shaped exception the rename abolishes.
* Small structured resources store their parsed payload in-row (``payload``
  JSONB) and never touch the blob store, so bucket/object_key become
  nullable with a CHECK that a row is exactly one of blob-backed or inline.

Sequences stay one shared space per session (UNIQUE(session_id, sequence)):
the streaming protocol's cumulative acks and resume point depend on it, and
a row's resource only types it.

DEPLOY NOTE: database/repos/segments.py matches the renamed
``resource_segments_session_id_sequence_key`` constraint name literally to
classify duplicate retransmits — this migration and that code must deploy
together, or every retransmit is misreported as a storage failure.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Postgres keeps constraint/index names across a table rename; rename them
    # explicitly so nothing keeps advertising the old table.
    op.execute("ALTER TABLE audio_segments RENAME TO resource_segments;")
    op.execute("""
        ALTER TABLE resource_segments
            RENAME CONSTRAINT audio_segments_pkey TO resource_segments_pkey;
    """)
    op.execute("""
        ALTER TABLE resource_segments
            RENAME CONSTRAINT audio_segments_session_id_sequence_key
            TO resource_segments_session_id_sequence_key;
    """)
    op.execute("""
        ALTER TABLE resource_segments
            RENAME CONSTRAINT audio_segments_object_key_key
            TO resource_segments_object_key_key;
    """)
    op.execute("""
        ALTER TABLE resource_segments
            RENAME CONSTRAINT audio_segments_session_id_user_id_fkey
            TO resource_segments_session_id_user_id_fkey;
    """)
    op.execute("""
        ALTER INDEX audio_segments_user_ingested_idx
            RENAME TO resource_segments_user_ingested_idx;
    """)

    op.execute("""
        ALTER TABLE resource_segments
            ADD COLUMN resource TEXT NOT NULL DEFAULT 'audio',
            ADD COLUMN attrs    JSONB NOT NULL DEFAULT '{}'::jsonb,
            ADD COLUMN payload  JSONB;
    """)

    # Every existing row is audio; fold its PCM columns into attrs, then drop
    # them. jsonb_strip_nulls keeps attrs empty for rows that declared nothing.
    op.execute("""
        UPDATE resource_segments
        SET attrs = jsonb_strip_nulls(jsonb_build_object(
                'codec', codec,
                'sample_rate_hz', sample_rate_hz,
                'channels', channels))
        WHERE codec IS NOT NULL OR sample_rate_hz IS NOT NULL OR channels IS NOT NULL;
    """)
    op.execute("""
        ALTER TABLE resource_segments
            DROP COLUMN codec,
            DROP COLUMN sample_rate_hz,
            DROP COLUMN channels;
    """)

    # byte_size/content_type/checksum_sha256 stay meaningful for inline rows
    # (they describe the wire payload), keeping usage accounting, stream acks
    # and checksum verification uniform across storage modes.
    op.execute("""
        ALTER TABLE resource_segments
            ALTER COLUMN bucket DROP NOT NULL,
            ALTER COLUMN object_key DROP NOT NULL;
    """)
    op.execute("""
        ALTER TABLE resource_segments
            ADD CONSTRAINT resource_segments_storage_check CHECK (
                (bucket IS NOT NULL AND object_key IS NOT NULL AND payload IS NULL)
             OR (bucket IS NULL AND object_key IS NULL AND payload IS NOT NULL)
            );
    """)

    # Session-scoped per-resource listing and time ranges; its (session_id,
    # resource) prefix also serves pipeline discovery's EXISTS probe.
    op.execute("""
        CREATE INDEX resource_segments_session_resource_captured_idx
            ON resource_segments (session_id, resource, captured_at);
    """)
    # Cross-session wall-clock location queries. Partial: audio dominates the
    # table (a chunk per second) and must not bloat it. Another wall-clock-
    # queryable resource adds its own partial index in its migration.
    op.execute("""
        CREATE INDEX resource_segments_location_wallclock_idx
            ON resource_segments (user_id, captured_at)
            WHERE resource = 'location';
    """)


def downgrade() -> None:
    # Lossy, like 0013: inline rows have no place in the audio-only shape.
    op.execute("DELETE FROM resource_segments WHERE payload IS NOT NULL;")
    op.execute("DROP INDEX resource_segments_location_wallclock_idx;")
    op.execute("DROP INDEX resource_segments_session_resource_captured_idx;")
    op.execute("""
        ALTER TABLE resource_segments
            DROP CONSTRAINT resource_segments_storage_check;
    """)
    op.execute("""
        ALTER TABLE resource_segments
            ALTER COLUMN bucket SET NOT NULL,
            ALTER COLUMN object_key SET NOT NULL;
    """)
    op.execute("""
        ALTER TABLE resource_segments
            ADD COLUMN codec TEXT,
            ADD COLUMN sample_rate_hz INTEGER,
            ADD COLUMN channels SMALLINT;
    """)
    op.execute("""
        UPDATE resource_segments
        SET codec = attrs->>'codec',
            sample_rate_hz = (attrs->>'sample_rate_hz')::integer,
            channels = (attrs->>'channels')::smallint
        WHERE attrs != '{}'::jsonb;
    """)
    op.execute("""
        ALTER TABLE resource_segments
            DROP COLUMN resource, DROP COLUMN attrs, DROP COLUMN payload;
    """)
    op.execute("""
        ALTER INDEX resource_segments_user_ingested_idx
            RENAME TO audio_segments_user_ingested_idx;
    """)
    op.execute("""
        ALTER TABLE resource_segments
            RENAME CONSTRAINT resource_segments_session_id_user_id_fkey
            TO audio_segments_session_id_user_id_fkey;
    """)
    op.execute("""
        ALTER TABLE resource_segments
            RENAME CONSTRAINT resource_segments_object_key_key
            TO audio_segments_object_key_key;
    """)
    op.execute("""
        ALTER TABLE resource_segments
            RENAME CONSTRAINT resource_segments_session_id_sequence_key
            TO audio_segments_session_id_sequence_key;
    """)
    op.execute("""
        ALTER TABLE resource_segments
            RENAME CONSTRAINT resource_segments_pkey TO audio_segments_pkey;
    """)
    op.execute("ALTER TABLE resource_segments RENAME TO audio_segments;")
