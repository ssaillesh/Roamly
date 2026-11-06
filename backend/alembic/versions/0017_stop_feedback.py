"""stop_feedback + venue_signals: what people keep or drop from plans, per stored venue,
and its daily summary that nudges the planner's ranking (services/feedback.py).

Revision ID: 0017_stop_feedback
Revises: 0016_shared_plans
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0017_stop_feedback"
down_revision = "0016_shared_plans"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if not insp.has_table("stop_feedback"):
        op.create_table(
            "stop_feedback",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("user_id", postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("vid", sa.String(80), nullable=False),
            sa.Column("slot", sa.String(20), nullable=True),
            sa.Column("event", sa.String(12), nullable=False),
            sa.Column("plan_key", sa.String(32), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint("user_id", "vid", "event", "plan_key", name="uq_stop_feedback_once"),
        )
        op.create_index("ix_stop_feedback_user_id", "stop_feedback", ["user_id"])
        op.create_index("ix_stop_feedback_vid", "stop_feedback", ["vid"])
        op.create_index("ix_stop_feedback_created_at", "stop_feedback", ["created_at"])
    if not insp.has_table("venue_signals"):
        op.create_table(
            "venue_signals",
            sa.Column("vid", sa.String(80), primary_key=True),
            sa.Column("users", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("positive", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("negative", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("score", sa.Float(), nullable=False, server_default="0.5"),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )


def downgrade() -> None:
    op.drop_table("venue_signals")
    op.drop_table("stop_feedback")
