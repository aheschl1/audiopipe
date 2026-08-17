"""agent query indexes: wall-clock session overlap

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-17

The MCP tools query sessions by wall-clock window ("what overlapped
[t0, t1)?"). ``recording_sessions_user_idx (user_id, started_at DESC)``
serves the upper bound, but for a narrow window over a long history the
selective bound is ``ended_at``. Btree indexes NULLs, so the open-session
arm (``ended_at IS NULL``) is served by the same index.

Artifact spans need nothing new: ``pipeline_artifacts_span_idx`` (0005)
already covers ``(session_id, start_ms)``, and the wall-clock artifact
filters only run after the session set is narrowed by this index.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX recording_sessions_user_ended_idx
            ON recording_sessions (user_id, ended_at);
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS recording_sessions_user_ended_idx;")
