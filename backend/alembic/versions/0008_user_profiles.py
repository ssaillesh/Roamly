"""add user_profiles (taste survey answers)

Revision ID: 0008_user_profiles
Revises: 0007_drop_trip_photos
Create Date: 2026-09-25
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = "0008_user_profiles"
down_revision = "0007_drop_trip_photos"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent: 0001's create_all() already builds this table on a fresh DB,
    # so only create it when missing (existing DBs get it here).
    bind = op.get_bind()
    if "user_profiles" not in sa.inspect(bind).get_table_names():
        op.create_table(
            "user_profiles",
            sa.Column("user_id", UUID(as_uuid=True),
                      sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("answers", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("survey_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )


def downgrade() -> None:
    op.drop_table("user_profiles")
