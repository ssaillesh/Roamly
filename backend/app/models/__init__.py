"""Import all models so Alembic/metadata can discover them."""
from app.models.user import User
from app.models.friendship import Friendship
from app.models.trip import Trip
from app.models.visited import VisitedCountry, VisitedCity
from app.models.badge import Badge, UserBadge
from app.models.challenge import Challenge, ChallengeParticipant
from app.models.activity import ActivityFeed
from app.models.waitlist import WaitlistSignup
from app.models.user_profile import TasteProfile

__all__ = [
    "WaitlistSignup",
    "TasteProfile",
    "User",
    "Friendship",
    "Trip",
    "VisitedCountry",
    "VisitedCity",
    "Badge",
    "UserBadge",
    "Challenge",
    "ChallengeParticipant",
    "ActivityFeed",
]
