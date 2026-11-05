"""venues.image: a photo for each stored venue, found by the monthly loader
(scripts/load_venues.py) — the venue website's own preview image (og:image),
else its Wikimedia Commons photo from Wikidata. "" = looked, found nothing;
NULL = not looked at yet.

Revision ID: 0015_venue_images
Revises: 0014_terms_acceptance
"""
from alembic import op
import sqlalchemy as sa

revision = "0015_venue_images"
down_revision = "0014_terms_acceptance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("venues")}
    if "image" not in cols:
        op.add_column("venues", sa.Column("image", sa.String(1000), nullable=True))


def downgrade() -> None:
    op.drop_column("venues", "image")
