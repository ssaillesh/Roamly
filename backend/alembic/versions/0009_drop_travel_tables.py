"""drop the TrekRank travel-logging tables and user columns

Roamly is an outing planner now. The old travel-logging features (trips, visited
places, badges, challenges, friends, activity feed, share cards) have no routes,
no screens and no code any more, so their tables and the cached per-user travel
stats go too. `apple_id` goes with them: Apple sign-in was removed earlier.

Idempotent like the rest of the chain: a fresh database (0001's create_all() from
the current models) never has these, so only what exists is dropped.

One-way: the dropped rows can't be rebuilt from code, so downgrade refuses —
restore a backup (pg_dump taken before this ran) instead.

Revision ID: 0009_drop_travel_tables
Revises: 0008_user_profiles
"""
from alembic import op
import sqlalchemy as sa

revision = "0009_drop_travel_tables"
down_revision = "0008_user_profiles"
branch_labels = None
depends_on = None

# Children before parents, so every foreign key is gone before its target.
TABLES = [
    "activity_feed",           # -> trips, badges, challenges, users
    "user_badges",             # -> badges, trips, users
    "challenge_participants",  # -> challenges, users
    "friendships",             # -> users
    "visited_cities",          # -> users
    "visited_countries",       # -> users
    "challenges",              # -> users
    "badges",
    "trips",                   # -> users
]
USER_COLUMNS = [
    "apple_id", "home_country", "featured_badges",
    "total_countries", "total_cities", "total_km", "total_trips",
    "current_streak", "longest_streak",
]


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    for table in TABLES:
        if table in existing:
            op.drop_table(table)
    columns = {c["name"] for c in sa.inspect(bind).get_columns("users")}
    for column in USER_COLUMNS:
        if column in columns:
            op.drop_column("users", column)     # its indexes go with it


def downgrade() -> None:
    raise RuntimeError(
        "0009_drop_travel_tables is one-way: the travel tables and their rows can't be "
        "rebuilt from code. Restore the pg_dump taken before upgrading instead.")
