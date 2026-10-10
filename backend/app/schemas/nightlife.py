from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.models.nightlife import EVENT_KINDS
from app.schemas.plan import Event


class SpotEvent(BaseModel):
    name: str
    start: str | None = None
    source: str | None = None                  # "seatgeek" → the page shows their logo with it


class Spot(BaseModel):
    """A place in the club index."""
    id: str
    name: str
    lat: float
    lng: float
    kind: str                                  # clubs | lgbtq | music | comedy | lounges | bars
    area: str | None = None
    distance_km: float | None = None
    categories: list[str] = []
    url: str | None = None
    address: str | None = None
    hours: str | None = None
    notability: float = 0
    open_tonight: bool | None = None           # None = hours unknown
    events: list[SpotEvent] = []


class SpecialDayOut(BaseModel):
    key: str
    name: str
    emoji: str
    blurb: str
    start: str
    end: str
    today: bool
    days_until: int


class Kind(BaseModel):
    key: str
    label: str
    count: int


class NightlifeResponse(BaseModel):
    window: str
    special_day: SpecialDayOut | None = None
    events: list[Event] = []
    themed_spots: list[Spot] = []
    spots: list[Spot] = []
    kinds: list[Kind] = []
    covered: bool = False                      # is this area in the stored venue list?
    ticketed: bool = False                     # is Ticketmaster connected?


def _http_url(v):
    if v and not str(v).lower().startswith(("http://", "https://")):
        raise ValueError("must be an http(s) link")
    return v or None


class EventFields(BaseModel):
    """What a venue submits / an admin fills in."""
    title: str = Field(min_length=3, max_length=160)
    venue_name: str = Field(min_length=2, max_length=160)
    starts_at: datetime
    ends_at: datetime | None = None
    kind: str = "club_night"
    price_min: float | None = Field(default=None, ge=0, le=5000)
    price_max: float | None = Field(default=None, ge=0, le=5000)
    free: bool | None = None
    url: str | None = Field(default=None, max_length=500)
    description: str | None = Field(default=None, max_length=500)
    special_day: str | None = Field(default=None, max_length=30)

    _url = field_validator("url")(_http_url)

    @field_validator("kind")
    @classmethod
    def _kind(cls, v):
        if v not in EVENT_KINDS:
            raise ValueError(f"kind must be one of {', '.join(EVENT_KINDS)}")
        return v


class SubmissionIn(EventFields):
    submitter_name: str | None = Field(default=None, max_length=120)
    submitter_email: EmailStr
    website: str | None = None                 # honeypot: people never see this field


class AdminEventIn(EventFields):
    venue_id: str | None = Field(default=None, max_length=80)   # link to a stored venue
    address: str | None = Field(default=None, max_length=300)
    lat: float | None = None
    lng: float | None = None
    status: str = "approved"


class AdminEventPatch(BaseModel):
    title: str | None = Field(default=None, min_length=3, max_length=160)
    venue_name: str | None = Field(default=None, min_length=2, max_length=160)
    venue_id: str | None = Field(default=None, max_length=80)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    kind: str | None = None
    price_min: float | None = Field(default=None, ge=0, le=5000)
    price_max: float | None = Field(default=None, ge=0, le=5000)
    free: bool | None = None
    url: str | None = Field(default=None, max_length=500)
    description: str | None = Field(default=None, max_length=500)
    special_day: str | None = Field(default=None, max_length=30)
    address: str | None = Field(default=None, max_length=300)
    lat: float | None = None
    lng: float | None = None

    _url = field_validator("url")(_http_url)
