"""admin dashboard: user flags, activity, plan logs, health, API usage, audit log

Adds is_admin / is_disabled / is_test / last_seen_at to users, invited_at to the
waitlist, and the analytics tables the admin dashboard reads from. Existing
smoke-test and seeded accounts are flagged is_test so they stay out of the stats.

Idempotent like the rest of the chain: 0001's create_all() builds all of this
from the current models on a fresh database, so only what's missing is added.

Revision ID: 0010_admin_analytics
Revises: 0009_drop_travel_tables
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = "0010_admin_analytics"
down_revision = "0009_drop_travel_tables"
branch_labels = None
depends_on = None

USER_FLAGS = ["is_admin", "is_disabled", "is_test"]
TABLES = ["user_activity_days", "plan_logs", "health_snapshots",
          "api_usage", "daily_rollups", "admin_audit_log"]

# Mirrors services/analytics.TEST_EMAIL_DOMAINS / looks_like_test().
TEST_BACKFILL = """
    UPDATE users SET is_test = true
    WHERE split_part(lower(email), '@', 2) IN
          ('example.com', 'example.org', 'example.net', 'ex.com',
           'test.trekrank.app', 'demo.trekrank.app')
       OR lower(email) LIKE '%.test'
       OR lower(username) LIKE 'smoke\\_%'
"""


def _ts(name, **kw):
    return sa.Column(name, sa.DateTime(timezone=True), **kw)


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    user_cols = {c["name"] for c in insp.get_columns("users")}
    for flag in USER_FLAGS:
        if flag not in user_cols:
            op.add_column("users", sa.Column(flag, sa.Boolean(), nullable=False,
                                             server_default=sa.false()))
    if "last_seen_at" not in user_cols:
        op.add_column("users", _ts("last_seen_at", nullable=True))
    op.execute(TEST_BACKFILL)

    if "invited_at" not in {c["name"] for c in insp.get_columns("waitlist_signups")}:
        op.add_column("waitlist_signups", _ts("invited_at", nullable=True))

    existing = set(insp.get_table_names())
    user_fk = lambda ondelete: sa.ForeignKey("users.id", ondelete=ondelete)  # noqa: E731

    if "user_activity_days" not in existing:
        op.create_table(
            "user_activity_days",
            sa.Column("user_id", UUID(as_uuid=True), user_fk("CASCADE"), primary_key=True),
            sa.Column("day", sa.Date(), primary_key=True),
        )
        op.create_index("ix_user_activity_days_day", "user_activity_days", ["day"])

    if "plan_logs" not in existing:
        op.create_table(
            "plan_logs",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("user_id", UUID(as_uuid=True), user_fk("CASCADE"), nullable=True),
            sa.Column("endpoint", sa.String(20), nullable=False),
            _ts("created_at", server_default=sa.func.now()),
            sa.Column("user_text", sa.Text(), nullable=True),
            sa.Column("prefs", JSONB(), nullable=True),
            sa.Column("lat", sa.Float(), nullable=True),
            sa.Column("lng", sa.Float(), nullable=True),
            sa.Column("area", sa.String(100), nullable=True),
            sa.Column("response_type", sa.String(20), nullable=True),
            sa.Column("stops", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("found_nothing", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("llm_calls", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("llm_failures", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("response", JSONB(), nullable=True),
        )
        op.create_index("ix_plan_logs_user_id", "plan_logs", ["user_id"])
        op.create_index("ix_plan_logs_created_at", "plan_logs", ["created_at"])

    if "health_snapshots" not in existing:
        op.create_table(
            "health_snapshots",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            _ts("ts", nullable=False),
            sa.Column("requests", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("status_4xx", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("status_5xx", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("p50_ms", sa.Float(), nullable=True),
            sa.Column("p95_ms", sa.Float(), nullable=True),
            sa.Column("routes", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("cold_start", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
        op.create_index("ix_health_snapshots_ts", "health_snapshots", ["ts"])

    if "api_usage" not in existing:
        op.create_table(
            "api_usage",
            sa.Column("day", sa.Date(), primary_key=True),
            sa.Column("provider", sa.String(40), primary_key=True),
            sa.Column("calls", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("errors", sa.Integer(), nullable=False, server_default="0"),
        )

    if "daily_rollups" not in existing:
        op.create_table(
            "daily_rollups",
            sa.Column("day", sa.Date(), primary_key=True),
            sa.Column("metric", sa.String(60), primary_key=True),
            sa.Column("key", sa.String(100), primary_key=True, server_default=""),
            sa.Column("value", sa.Float(), nullable=False, server_default="0"),
        )

    if "admin_audit_log" not in existing:
        op.create_table(
            "admin_audit_log",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("admin_id", UUID(as_uuid=True), user_fk("SET NULL"), nullable=True),
            sa.Column("action", sa.String(40), nullable=False),
            sa.Column("target", sa.String(255), nullable=True),
            sa.Column("details", JSONB(), nullable=True),
            _ts("created_at", server_default=sa.func.now()),
        )
        op.create_index("ix_admin_audit_log_created_at", "admin_audit_log", ["created_at"])


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)
    op.drop_column("waitlist_signups", "invited_at")
    for column in [*USER_FLAGS, "last_seen_at"]:
        op.drop_column("users", column)
