"""tutor: sessions, study settings and plans, the model memo, section and document summaries

LangGraph's checkpoint tables are not created here: `AsyncPostgresSaver.setup()` makes them at API startup,
because its migrations use CREATE INDEX CONCURRENTLY. The downgrade drops them so the schema goes cleanly.

Revision ID: 0008
Revises: 0007
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

CHECKPOINT_TABLES = ("checkpoint_writes", "checkpoint_blobs", "checkpoints", "checkpoint_migrations")


def upgrade() -> None:
    now = sa.func.now()
    roadmap_fk = lambda: sa.ForeignKey("roadmaps.id", ondelete="CASCADE")  # noqa: E731
    op.create_table(
        "tutor_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("roadmap_id", postgresql.UUID(as_uuid=True), roadmap_fk(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("curated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_kept", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=False, server_default="0"),
        sa.CheckConstraint("mode IN ('tutor', 'qa')", name="ck_tutor_sessions_mode"),
        sa.CheckConstraint("status IN ('active', 'ended')", name="ck_tutor_sessions_status"),
    )
    op.create_index("ix_tutor_sessions_roadmap_id", "tutor_sessions", ["roadmap_id"])
    op.create_table(
        "study_settings",
        sa.Column("roadmap_id", postgresql.UUID(as_uuid=True), roadmap_fk(), primary_key=True),
        sa.Column("weekly_hours", sa.Float(), nullable=True),
        sa.Column("session_minutes", sa.Integer(), nullable=False, server_default="45"),
        sa.Column("study_days", postgresql.JSONB(), nullable=False, server_default="[0,1,2,3,4]"),
        sa.Column("deadline", sa.Date(), nullable=True),
    )
    op.create_table(
        "study_plans",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("roadmap_id", postgresql.UUID(as_uuid=True), roadmap_fk(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("plan", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.UniqueConstraint("roadmap_id", "version"),
        sa.CheckConstraint("status IN ('active', 'proposed', 'superseded')", name="ck_study_plans_status"),
    )
    op.create_table(
        "llm_memo",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("response", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
    )
    op.add_column("sections", sa.Column("summary", sa.Text(), nullable=True))
    op.add_column("sections", sa.Column("quiz", postgresql.JSONB(), nullable=True))
    op.add_column("documents", sa.Column("summary", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("documents", "summary")
    op.drop_column("sections", "quiz")
    op.drop_column("sections", "summary")
    for table in ("llm_memo", "study_plans", "study_settings", "tutor_sessions"):
        op.drop_table(table)
    for table in CHECKPOINT_TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table}")
