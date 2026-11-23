"""ingestion agent: agent_actions; proposed resources

Revision ID: 0003
Revises: 0002
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_actions",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("roadmap_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("roadmaps.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("round", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tool", sa.Text(), nullable=False),
        sa.Column("args", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_agent_actions_roadmap_id", "agent_actions", ["roadmap_id"])
    op.add_column("resources", sa.Column("meta", postgresql.JSONB(), nullable=False, server_default="{}"))
    op.add_column("resources", sa.Column("proposed_by", sa.Text(), nullable=True))
    op.add_column("resources", sa.Column("proposal_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.execute("DELETE FROM resources WHERE status IN ('proposed', 'dismissed')")
    op.drop_column("resources", "proposal_reason")
    op.drop_column("resources", "proposed_by")
    op.drop_column("resources", "meta")
    op.drop_index("ix_agent_actions_roadmap_id", table_name="agent_actions")
    op.drop_table("agent_actions")
