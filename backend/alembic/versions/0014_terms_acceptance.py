"""users.terms_accepted_at / terms_version: when someone accepted the Terms of
Service and Privacy Policy, and which version (SeatGeek's API terms require users
to accept an end-user agreement; it's good practice anyway)

Revision ID: 0014_terms_acceptance
Revises: 0013_nightlife_events
"""
from alembic import op
import sqlalchemy as sa

revision = "0014_terms_acceptance"
down_revision = "0013_nightlife_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("users")}
    if "terms_accepted_at" not in cols:
        op.add_column("users", sa.Column("terms_accepted_at", sa.DateTime(timezone=True), nullable=True))
    if "terms_version" not in cols:
        op.add_column("users", sa.Column("terms_version", sa.String(20), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "terms_version")
    op.drop_column("users", "terms_accepted_at")
