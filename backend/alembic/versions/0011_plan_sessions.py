"""plan_logs: session_id + intent, so a whole conversation can be read together

Revision ID: 0011_plan_sessions
Revises: 0010_admin_analytics
"""
from alembic import op
import sqlalchemy as sa

revision = "0011_plan_sessions"
down_revision = "0010_admin_analytics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent: 0001's create_all() builds plan_logs with these on a fresh DB.
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("plan_logs")}
    if "session_id" not in cols:
        op.add_column("plan_logs", sa.Column("session_id", sa.String(64), nullable=True))
        op.create_index("ix_plan_logs_session_id", "plan_logs", ["session_id"])
    if "intent" not in cols:
        op.add_column("plan_logs", sa.Column("intent", sa.String(20), nullable=True))


def downgrade() -> None:
    op.drop_index("ix_plan_logs_session_id", table_name="plan_logs")
    op.drop_column("plan_logs", "intent")
    op.drop_column("plan_logs", "session_id")
