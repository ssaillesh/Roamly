"""init: users table

The planner is members-only, so accounts are the only persisted state. Every
other table (trips, badges, friendships, leaderboards, challenges, feed,
waitlist) belonged to the travel-logging product and was removed.

Revision ID: 0001_init
Revises:
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_init"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("apple_id", sa.String(255), nullable=True, unique=True),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("username", sa.String(30), nullable=False, unique=True),
        sa.Column("display_name", sa.String(100), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_users_username", "users", ["username"])


def downgrade() -> None:
    op.drop_index("ix_users_username", table_name="users")
    op.drop_table("users")
