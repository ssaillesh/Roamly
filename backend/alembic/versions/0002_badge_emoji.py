"""add badges.emoji column

Revision ID: 0002_badge_emoji
Revises: 0001_init
Create Date: 2026-06-25
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_badge_emoji"
down_revision = "0001_init"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent: a fresh DB's 0001 migration runs Base.metadata.create_all()
    # against the *current* models, which already include this column. Only add
    # it when missing so the chain also applies cleanly to older databases.
    bind = op.get_bind()
    # badges itself is gone from the models as of 0009, so a fresh DB never
    # creates it at all — skip (same pattern as trip_photos in 0005).
    if "badges" not in sa.inspect(bind).get_table_names():
        return
    cols = [c["name"] for c in sa.inspect(bind).get_columns("badges")]
    if "emoji" not in cols:
        op.add_column("badges", sa.Column("emoji", sa.String(length=16), nullable=True))


def downgrade() -> None:
    op.drop_column("badges", "emoji")
