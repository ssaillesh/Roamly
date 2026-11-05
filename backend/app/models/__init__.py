"""Import all models so Alembic/metadata can discover them.

Only what Rova uses: accounts, taste profiles, the waitlist, the admin
dashboard's analytics tables, the stored venue list and Rova's own nightlife events. The old TrekRank travel-logging tables were
dropped in migration 0009.
"""
from app.models.user import User
from app.models.waitlist import WaitlistSignup
from app.models.user_profile import TasteProfile
from app.models.venue import Venue, Area
from app.models.nightlife import NightlifeEvent
from app.models.shared_plan import SharedPlan
from app.models.analytics import (
    UserActivityDay, PlanLog, HealthSnapshot, ApiUsage, DailyRollup, AdminAuditLog,
)

__all__ = [
    "User",
    "WaitlistSignup",
    "TasteProfile",
    "UserActivityDay",
    "PlanLog",
    "HealthSnapshot",
    "ApiUsage",
    "DailyRollup",
    "AdminAuditLog",
    "Venue",
    "Area",
    "NightlifeEvent",
    "SharedPlan",
]
