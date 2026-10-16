"""The Nightlife tab (/nightlife) and the public "list your event" form."""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.middleware.auth import get_current_user
from app.middleware.rate_limit import client_ip
from app.models import NightlifeEvent, User
from app.redis_client import redis_client
from app.schemas.nightlife import NightlifeResponse, SubmissionIn
from app.services import nightlife, special_days, venue_index
from app.services.geocoding import geocode

router = APIRouter(prefix="/nightlife", tags=["nightlife"])

TORONTO = (43.6532, -79.3832)
SUBMISSIONS_PER_DAY = 5          # per address; nothing shows before an admin approves it anyway
MAX_DAYS_AHEAD = 180


@router.get("", response_model=NightlifeResponse)
def get_nightlife(lat: float | None = None, lng: float | None = None,
                  location: str | None = Query(default=None, max_length=120),
                  radius_km: float = Query(default=10, ge=1, le=25),
                  window: str = Query(default="tonight", pattern="^(tonight|weekend|week)$"),
                  tz: str | None = Query(default=None, max_length=60),
                  user: User = Depends(get_current_user)):
    if location:
        found = venue_index.find_area(location, (lat, lng)) or geocode(location, None)
        if found:
            lat, lng = found
    if lat is None or lng is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Set a location first.")
    return nightlife.feed(lat, lng, window=window, radius_km=radius_km, tz=tz)


def _as_utc(dt: datetime) -> datetime:
    """A time typed without a zone is Toronto time."""
    return (dt if dt.tzinfo else dt.replace(tzinfo=special_days.TZ)).astimezone(timezone.utc)


def check_times(starts_at: datetime, ends_at: datetime | None) -> tuple[datetime, datetime | None]:
    start, end = _as_utc(starts_at), (_as_utc(ends_at) if ends_at else None)
    now = datetime.now(timezone.utc)
    if start < now - timedelta(hours=6):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "That date has already passed.")
    if start > now + timedelta(days=MAX_DAYS_AHEAD):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Events can be listed up to 6 months ahead.")
    if end and end <= start:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "The end time must be after the start.")
    return start, end


def link_venue(row: NightlifeEvent, near: tuple[float, float] = TORONTO) -> None:
    """Attach the stored place with this name (for the map, distance and the club index)."""
    v = venue_index.find_venue(row.venue_name, near[0], near[1], radius_m=30000)
    if v:
        row.venue_id, row.lat, row.lng = v["id"], v["lat"], v["lng"]
        row.address = row.address or v.get("address")


@router.post("/submissions", status_code=status.HTTP_202_ACCEPTED)
def submit_event(body: SubmissionIn, request: Request, db: Session = Depends(get_db)):
    """A venue or promoter lists an event. It waits for an admin's approval."""
    if body.website:                                   # the honeypot was filled: a bot
        return {"ok": True}
    # The address our proxies saw: the left of X-Forwarded-For is the client's to fake.
    key = f"nl_submit:{client_ip(request)}:{datetime.now(timezone.utc):%Y%m%d}"
    try:
        n = redis_client.incr(key)
        redis_client.expire(key, 86400)
    except Exception:
        n = 0
    if n > SUBMISSIONS_PER_DAY:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "That's a lot of events for one day — try again tomorrow.")
    start, end = check_times(body.starts_at, body.ends_at)
    row = NightlifeEvent(
        title=body.title.strip(), venue_name=body.venue_name.strip(), starts_at=start, ends_at=end,
        kind=body.kind, price_min=body.price_min, price_max=body.price_max, free=body.free, url=body.url,
        description=(body.description or "").strip() or None,
        special_day=body.special_day if body.special_day in special_days.BY_KEY else None,
        status="pending", source="submission", submitter_name=body.submitter_name,
        submitter_email=str(body.submitter_email))
    link_venue(row)
    db.add(row)
    db.commit()
    return {"ok": True}
