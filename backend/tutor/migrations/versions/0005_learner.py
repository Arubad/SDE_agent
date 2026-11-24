"""learner: consent settings, per-skill progress, semantic facts

Revision ID: 0005
Revises: 0004
"""
import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    now = sa.func.now()
    settings = op.create_table(
        "learner_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("consent_personal", sa.Boolean(), nullable=True),
        sa.Column("consent_raw_messages", sa.Boolean(), nullable=True),
        sa.Column("retention_days", sa.Integer(), nullable=False, server_default="90"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.CheckConstraint("id = 1", name="ck_learner_settings_singleton"),
    )
    op.bulk_insert(settings, [{"id": 1}])
    op.create_table(
        "skill_progress",
        sa.Column("roadmap_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("roadmaps.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("skill_id", sa.Text(), primary_key=True),
        sa.Column("mastery", sa.Float(), nullable=False, server_default="0"),
        sa.Column("weak_spots", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("next_review_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("unlock_threshold", sa.Float(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.CheckConstraint("mastery >= 0 AND mastery <= 1", name="ck_skill_progress_mastery"),
    )
    op.create_table(
        "learner_facts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("roadmap_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("roadmaps.id", ondelete="CASCADE"), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(), nullable=False),
        sa.Column("embedder", sa.Text(), nullable=False),
        sa.Column("provenance", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.CheckConstraint("kind IN ('fact', 'preference', 'goal')", name="ck_learner_facts_kind"),
    )


def downgrade() -> None:
    op.drop_table("learner_facts")
    op.drop_table("skill_progress")
    op.drop_table("learner_settings")
