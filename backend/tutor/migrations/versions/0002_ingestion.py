"""ingestion: resources, jobs, documents, sections, http_cache

Revision ID: 0002
Revises: 0001
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

_UUID = postgresql.UUID(as_uuid=True)
_ROADMAP_FK = lambda: sa.ForeignKey("roadmaps.id", ondelete="CASCADE")  # noqa: E731
_NOW = sa.func.now()


def upgrade() -> None:
    op.create_table(
        "resources",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("roadmap_id", _UUID, _ROADMAP_FK(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("stage_id", sa.Text(), nullable=False),
        sa.Column("skill_ids", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("focus", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("stats", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.UniqueConstraint("roadmap_id", "key"),
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("roadmap_id", _UUID, _ROADMAP_FK(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False, unique=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.Column("parent_id", sa.BigInteger(), sa.ForeignKey("jobs.id"), nullable=True),
        sa.Column("locked_by", sa.Text(), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
    )
    op.create_index("ix_jobs_status_not_before", "jobs", ["status", "not_before"])
    op.create_table(
        "documents",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("roadmap_id", _UUID, _ROADMAP_FK(), nullable=False),
        sa.Column("resource_id", _UUID, sa.ForeignKey("resources.id", ondelete="CASCADE"), nullable=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False, server_default=""),
        sa.Column("content_hash", sa.Text(), nullable=True),
        sa.Column("markdown", sa.Text(), nullable=False, server_default=""),
        sa.Column("meta", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("status", sa.Text(), nullable=False, server_default="ok"),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.UniqueConstraint("roadmap_id", "url"),
    )
    op.create_table(
        "sections",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("roadmap_id", _UUID, _ROADMAP_FK(), nullable=False),
        sa.Column("document_id", _UUID, sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("skill_ids", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("path", sa.Text(), nullable=False, server_default=""),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("start_s", sa.Float(), nullable=True),
        sa.Column("end_s", sa.Float(), nullable=True),
        sa.Column("ord", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_table(
        "http_cache",
        sa.Column("url", sa.Text(), primary_key=True),
        sa.Column("status", sa.Integer(), nullable=False),
        sa.Column("headers", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("body", sa.LargeBinary(), nullable=False),
        sa.Column("etag", sa.Text(), nullable=True),
        sa.Column("last_modified", sa.Text(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
    )


def downgrade() -> None:
    op.drop_table("http_cache")
    op.drop_table("sections")
    op.drop_table("documents")
    op.drop_index("ix_jobs_status_not_before", table_name="jobs")
    op.drop_table("jobs")
    op.drop_table("resources")
