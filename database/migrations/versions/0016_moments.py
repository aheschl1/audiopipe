"""moments and the artifact link graph

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-15

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Per user and on the wall clock, not per session: a moment spans every
    # session that recorded in it. metadata holds the model's description and
    # a snapshot of the labelled items it was shown.
    op.execute("""
        CREATE TABLE moments (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            start_at    TIMESTAMPTZ NOT NULL,
            end_at      TIMESTAMPTZ NOT NULL,
            title       TEXT NOT NULL,
            summary     TEXT NOT NULL,
            session_ids UUID[] NOT NULL,
            model       TEXT NOT NULL,
            metadata    JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            CHECK (end_at >= start_at)
        );
    """)
    op.execute("CREATE INDEX moments_user_start_idx ON moments (user_id, start_at DESC);")
    # Edges between artifacts, owned by the moment that asserted them: a
    # rebuild replaces its moment and so its links.
    op.execute("""
        CREATE TABLE artifact_links (
            moment_id     UUID NOT NULL REFERENCES moments(id) ON DELETE CASCADE,
            from_artifact UUID NOT NULL REFERENCES pipeline_artifacts(id) ON DELETE CASCADE,
            to_artifact   UUID NOT NULL REFERENCES pipeline_artifacts(id) ON DELETE CASCADE,
            relation      TEXT NOT NULL,
            confidence    REAL NOT NULL,
            why           TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (moment_id, from_artifact, to_artifact, relation)
        );
    """)
    op.execute("CREATE INDEX artifact_links_from_idx ON artifact_links (from_artifact);")
    op.execute("CREATE INDEX artifact_links_to_idx ON artifact_links (to_artifact);")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS artifact_links;")
    op.execute("DROP TABLE IF EXISTS moments;")
