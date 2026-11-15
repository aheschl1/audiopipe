"""keyframe image embeddings in pgvector

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-15

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # One row per keyframe artifact, same shape as audio_embeddings (0008):
    # keyed by the artifact so deleting it or its session takes the vector too.
    op.execute("""
        CREATE TABLE visual_embeddings (
            artifact_id UUID PRIMARY KEY
                REFERENCES pipeline_artifacts(id) ON DELETE CASCADE,
            session_id  UUID NOT NULL,
            model       TEXT NOT NULL,
            embedding   vector NOT NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """)
    op.execute("CREATE INDEX visual_embeddings_session_idx ON visual_embeddings (session_id);")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS visual_embeddings;")
