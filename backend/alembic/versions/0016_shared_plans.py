"""shared_plans: plans shared with "Share the night", readable by anyone with
the link (/n/<code>). Deleted with the account that shared them.

Revision ID: 0016_shared_plans
Revises: 0015_venue_images
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0016_shared_plans"
down_revision = "0015_venue_images"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("shared_plans"):
        return
    op.create_table(
        "shared_plans",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("code", sa.String(16), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("plan", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_shared_plans_code", "shared_plans", ["code"], unique=True)
    op.create_index("ix_shared_plans_user_id", "shared_plans", ["user_id"])
    op.create_index("ix_shared_plans_user_digest", "shared_plans", ["user_id", "digest"])


def downgrade() -> None:
    op.drop_table("shared_plans")
