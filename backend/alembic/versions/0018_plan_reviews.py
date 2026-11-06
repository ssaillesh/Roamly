"""plan_reviews: 1–5 star ratings and comments on generated plans, for the admin
Reviews page.

Revision ID: 0018_plan_reviews
Revises: 0017_stop_feedback
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0018_plan_reviews"
down_revision = "0017_stop_feedback"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("plan_reviews"):
        return
    op.create_table(
        "plan_reviews",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("plan_key", sa.String(32), nullable=False),
        sa.Column("stars", sa.SmallInteger(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("plan", postgresql.JSONB(), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "plan_key", name="uq_plan_review_once"),
        sa.CheckConstraint("stars BETWEEN 1 AND 5", name="ck_plan_review_stars"),
    )
    op.create_index("ix_plan_reviews_user_id", "plan_reviews", ["user_id"])
    op.create_index("ix_plan_reviews_created_at", "plan_reviews", ["created_at"])


def downgrade() -> None:
    op.drop_table("plan_reviews")
