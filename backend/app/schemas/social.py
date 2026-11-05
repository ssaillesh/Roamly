from datetime import datetime
from pydantic import BaseModel, Field

from app.schemas.user import UserPublic


# ---- Friends ----
class FriendRequestCreate(BaseModel):
    username: str


class FriendshipOut(BaseModel):
    friendship_id: str
    status: str
    addressee: UserPublic | None = None
    requester: UserPublic | None = None


class FriendOut(BaseModel):
    friendship_id: str
    user: UserPublic
    status: str
    since: datetime | None = None


# ---- Leaderboards ----
class LeaderboardEntry(BaseModel):
    rank: int
    user: UserPublic
    value: float
    trend: str = "same"


class LeaderboardResponse(BaseModel):
    metric: str
    period: str
    rankings: list[LeaderboardEntry]
    my_rank: int | None = None


# ---- Feed ----
class FeedTrip(BaseModel):
    id: str
    title: str | None = None
    dest_city: str
    dest_country: str
    distance_km: float | None = None


class FeedBadge(BaseModel):
    id: str
    name: str
    icon_url: str | None = None


class FeedRecommendation(BaseModel):
    text: str
    city: str | None = None
    country: str | None = None


class RecommendationCreate(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    city: str | None = Field(default=None, max_length=100)
    country: str | None = Field(default=None, max_length=100)


class FeedItem(BaseModel):
    id: str
    event_type: str
    user: UserPublic
    trip: FeedTrip | None = None
    badge: FeedBadge | None = None
    recommendation: FeedRecommendation | None = None
    created_at: datetime


class FeedResponse(BaseModel):
    items: list[FeedItem]
    next_cursor: str | None = None


# ---- Badges ----
class BadgeOut(BaseModel):
    id: str
    name: str
    description: str
    icon_url: str | None = None
    emoji: str | None = None
    category: str
    requirement: dict
    earned: bool = False
    earned_at: datetime | None = None


# ---- Challenges ----
class ChallengeCreate(BaseModel):
    title: str
    description: str | None = None
    challenge_type: str  # countries | cities | km | trips
    target_value: int
    start_date: str
    end_date: str
    invite_usernames: list[str] = []


class ChallengeParticipantOut(BaseModel):
    user: UserPublic
    progress: int
    completed: bool


class ChallengeOut(BaseModel):
    id: str
    title: str
    description: str | None = None
    challenge_type: str
    target_value: int
    start_date: str
    end_date: str
    is_global: bool
    participants: list[ChallengeParticipantOut] = []
    my_progress: int | None = None
