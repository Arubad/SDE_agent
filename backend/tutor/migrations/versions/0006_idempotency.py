"""mcp_idempotency: claimed idempotent MCP writes

Revision ID: 0006
Revises: 0005
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_idempotency",
        sa.Column("tool", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("args_hash", sa.Text(), nullable=False),
        sa.Column("result", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("tool", "key"),
    )


def downgrade() -> None:
    op.drop_table("mcp_idempotency")
