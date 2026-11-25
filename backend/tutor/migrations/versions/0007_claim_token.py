"""mcp_idempotency.claim_token: only the holder of a claim may store or release it

Revision ID: 0007
Revises: 0006
"""
import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("mcp_idempotency", sa.Column("claim_token", sa.Text(), nullable=True))
    op.execute("UPDATE mcp_idempotency SET claim_token = md5(random()::text)")
    op.alter_column("mcp_idempotency", "claim_token", nullable=False)


def downgrade() -> None:
    op.drop_column("mcp_idempotency", "claim_token")
