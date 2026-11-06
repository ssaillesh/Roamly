"""Shared plans ("Share the night"). The plan comes from the browser, so only
known fields are kept, every string is length-capped and links must be http(s):
it is shown to strangers on /n/<code> and drawn on the story card."""
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _clip(n: int):
    def f(v):
        if v is None:
            return None
        v = " ".join(str(v).split())
        return v[:n] or None
    return f


class SharedStop(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=200)
    vid: str | None = Field(default=None, max_length=80)     # the stored venue, for services/feedback.py
    time: str | None = None
    slot: str | None = None
    label: str | None = None
    categories: list[str] = Field(default_factory=list)
    address: str | None = None
    why: str | None = None
    price: str | None = None
    image: str | None = None
    url: str | None = None
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)
    est_cost: float | None = Field(default=None, ge=0, le=10000)
    rating: float | None = Field(default=None, ge=0, le=10)

    _name = field_validator("name", mode="before")(_clip(120))
    _time = field_validator("time", mode="before")(_clip(20))
    _slot = field_validator("slot", mode="before")(_clip(20))
    _label = field_validator("label", mode="before")(_clip(60))
    _address = field_validator("address", mode="before")(_clip(200))
    _why = field_validator("why", mode="before")(_clip(400))
    _price = field_validator("price", mode="before")(_clip(8))

    @field_validator("categories", mode="before")
    @classmethod
    def _cats(cls, v):
        if not isinstance(v, list):
            return []
        return [c for c in (_clip(40)(x) for x in v[:4]) if c]

    @field_validator("image", mode="before")
    @classmethod
    def _image(cls, v):
        # Drawn server-side on the card: https only.
        v = _clip(500)(v)
        return v if v and v.lower().startswith("https://") else None

    @field_validator("url", mode="before")
    @classmethod
    def _url(cls, v):
        v = _clip(500)(v)
        return v if v and v.lower().startswith(("https://", "http://")) else None


class SharedPlanIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str = Field(min_length=1, max_length=200)
    area: str | None = None
    date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    party_size: int = Field(default=1, ge=1, le=20)
    estimated_cost: float = Field(default=0, ge=0, le=200000)
    budget: float | None = Field(default=None, ge=0, le=200000)
    walk_km: float | None = Field(default=None, ge=0, le=500)
    transport: str | None = None
    tip: str | None = None
    stops: list[SharedStop] = Field(min_length=1, max_length=12)

    _title = field_validator("title", mode="before")(_clip(120))
    _area = field_validator("area", mode="before")(_clip(80))
    _transport = field_validator("transport", mode="before")(_clip(12))
    _tip = field_validator("tip", mode="before")(_clip(400))


class ShareLinkOut(BaseModel):
    code: str
    url: str                 # the public page, /n/<code> on the web app
    image_path: str          # the story card, relative to the API base (/api/v1)


class SharedPlanOut(BaseModel):
    code: str
    title: str
    plan: dict
    created_at: datetime
