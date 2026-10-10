"""venues + areas: the stored venue list the planner picks from

Loaded monthly by scripts/load_venues.py (Overture Maps + Wikidata + OSM), so
plans don't depend on a live map search.

Revision ID: 0012_venues
Revises: 0011_plan_sessions
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0012_venues"
down_revision = "0011_plan_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())   # 0001's create_all may have made them
    if "venues" not in existing:
        op.create_table(
            "venues",
            sa.Column("id", sa.String(80), primary_key=True),
            sa.Column("city", sa.String(40), nullable=False),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("lat", sa.Float(), nullable=False),
            sa.Column("lng", sa.Float(), nullable=False),
            sa.Column("slots", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
            sa.Column("category", sa.String(80), nullable=False),
            sa.Column("categories", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
            sa.Column("chain", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("fast_food", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("website", sa.String(500), nullable=True),
            sa.Column("address", sa.String(300), nullable=True),
            sa.Column("hours", sa.String(300), nullable=True),
            sa.Column("notability", sa.Float(), nullable=False, server_default="0"),
            sa.Column("wikidata", sa.String(20), nullable=True),
            sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_venues_lat_lng", "venues", ["lat", "lng"])     # lookups are by area, not city
    if "areas" not in existing:
        op.create_table(
            "areas",
            sa.Column("id", sa.String(80), primary_key=True),
            sa.Column("city", sa.String(40), nullable=False),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("kind", sa.String(20), nullable=False),
            sa.Column("lat", sa.Float(), nullable=False),
            sa.Column("lng", sa.Float(), nullable=False),
        )
        op.create_index("ix_areas_name", "areas", ["name"])


def downgrade() -> None:
    op.drop_table("areas")
    op.drop_table("venues")
