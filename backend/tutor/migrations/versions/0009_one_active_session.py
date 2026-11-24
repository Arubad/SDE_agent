"""at most one active tutor session per roadmap

Revision ID: 0009
Revises: 0008
"""
import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("uq_tutor_sessions_one_active", "tutor_sessions", ["roadmap_id"], unique=True,
                    postgresql_where=sa.text("status = 'active'"))


def downgrade() -> None:
    op.drop_index("uq_tutor_sessions_one_active", table_name="tutor_sessions")
