"""Import all models so Alembic/metadata can discover them.

Only what Roamly uses: accounts, taste profiles and the waitlist. The old
TrekRank travel-logging tables were dropped in migration 0009.
"""
from app.models.user import User
from app.models.waitlist import WaitlistSignup
from app.models.user_profile import TasteProfile

__all__ = [
    "User",
    "WaitlistSignup",
    "TasteProfile",
]
