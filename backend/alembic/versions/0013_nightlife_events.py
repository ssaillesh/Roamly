"""nightlife_events: club nights and special events Roamly lists itself
(added by an admin, or submitted by a venue and approved by an admin)

Revision ID: 0013_nightlife_events
Revises: 0012_venues
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0013_nightlife_events"
down_revision = "0012_venues"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "nightlife_events" in sa.inspect(op.get_bind()).get_table_names():   # 0001's create_all
        return
    op.create_table(
        "nightlife_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("venue_name", sa.String(160), nullable=False),
        sa.Column("venue_id", sa.String(80), nullable=True),
        sa.Column("lat", sa.Float(), nullable=True),
        sa.Column("lng", sa.Float(), nullable=True),
        sa.Column("address", sa.String(300), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("price_min", sa.Float(), nullable=True),
        sa.Column("price_max", sa.Float(), nullable=True),
        sa.Column("free", sa.Boolean(), nullable=True),
        sa.Column("url", sa.String(500), nullable=True),
        sa.Column("kind", sa.String(20), nullable=False, server_default="club_night"),
        sa.Column("special_day", sa.String(30), nullable=True),
        sa.Column("description", sa.String(500), nullable=True),
        sa.Column("status", sa.String(10), nullable=False, server_default="pending"),
        sa.Column("source", sa.String(12), nullable=False, server_default="admin"),
        sa.Column("submitter_name", sa.String(120), nullable=True),
        sa.Column("submitter_email", sa.String(254), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("reviewed_by", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_nightlife_events_starts_at", "nightlife_events", ["starts_at"])
    op.create_index("ix_nightlife_events_status", "nightlife_events", ["status"])


def downgrade() -> None:
    op.drop_table("nightlife_events")
