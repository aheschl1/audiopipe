"""automations: user rules that run on new moments, and their runs

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-15

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # conditions and actions are validated by database.schema.automations.
    # active_since bounds which moments a rule sees: only those created while
    # it was on, so enabling a rule never sweeps the whole history.
    op.execute("""
        CREATE TABLE automations (
            id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name         TEXT NOT NULL,
            enabled      BOOLEAN NOT NULL DEFAULT true,
            trigger      TEXT NOT NULL,
            conditions   JSONB NOT NULL DEFAULT '{}'::jsonb,
            actions      JSONB NOT NULL DEFAULT '[]'::jsonb,
            active_since TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """)
    op.execute("CREATE INDEX automations_user_idx ON automations (user_id, created_at);")
    # One run per rule and moment. A rebuilt moment is a new row, so it runs again.
    op.execute("""
        CREATE TABLE automation_runs (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            automation_id UUID NOT NULL REFERENCES automations(id) ON DELETE CASCADE,
            moment_id     UUID NOT NULL REFERENCES moments(id) ON DELETE CASCADE,
            status        TEXT NOT NULL CHECK (status IN ('matched', 'skipped', 'failed')),
            output        JSONB NOT NULL DEFAULT '{}'::jsonb,
            error         TEXT,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (automation_id, moment_id)
        );
    """)
    op.execute("CREATE INDEX automation_runs_moment_idx ON automation_runs (moment_id);")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS automation_runs;")
    op.execute("DROP TABLE IF EXISTS automations;")
