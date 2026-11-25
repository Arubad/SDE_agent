"""drop curator replies from llm_memo; tutor_sessions.last_turn_at

The curator no longer memoises (R17): its old replies, recognisable by the `resolved_weak_spots` key, may hold
personal facts outside consent, so they are deleted. `last_turn_at` lets the retention loop end idle sessions (R19).

Revision ID: 0010
Revises: 0009
"""
import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DELETE FROM llm_memo WHERE response ? 'resolved_weak_spots'")
    op.add_column("tutor_sessions", sa.Column("last_turn_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    # The deleted memo rows are not restored: they were a cache, and the reason they went.
    op.drop_column("tutor_sessions", "last_turn_at")
