from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User, VisitedCountry, VisitedCity, Badge, UserBadge, TasteProfile
from app.middleware.auth import get_current_user
from app.schemas.user import (
    UserProfile, UserPublic, UserUpdate, UserStats, UserMap, MapCountry, MapCity,
    FeaturedBadgesUpdate,
)
from app.schemas.profile import SurveyOut, TasteProfileOut, TasteProfileUpdate
from app.schemas.social import BadgeOut
from app.services import taste_profile
from app.services.stats import detailed_stats

router = APIRouter(prefix="/users", tags=["users"])


def _profile(user: User, include_email: bool = False) -> UserProfile:
    return UserProfile(
        id=str(user.id), username=user.username, display_name=user.display_name,
        avatar_url=user.avatar_url, bio=user.bio, home_city=user.home_city,
        home_country=user.home_country,
        email=user.email if include_email else None,
        featured_badges=user.featured_badges or [],
        total_countries=user.total_countries, total_cities=user.total_cities,
        total_km=float(user.total_km), total_trips=user.total_trips,
        current_streak=user.current_streak, longest_streak=user.longest_streak,
    )


def _get_by_username(db: Session, username: str) -> User:
    user = db.scalar(select(User).where(User.username == username))
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return user


@router.get("/me", response_model=UserProfile)
def get_me(user: User = Depends(get_current_user)):
    return _profile(user, include_email=True)


@router.patch("/me", response_model=UserProfile)
def update_me(body: UserUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    data = body.model_dump(exclude_unset=True)
    if "email" in data and data["email"] and data["email"] != user.email:
        if db.scalar(select(User.id).where(User.email == data["email"])):
            raise HTTPException(status.HTTP_409_CONFLICT, "Email already in use")
    home_changed = "home_country" in data and data["home_country"] != user.home_country
    for field, value in data.items():
        setattr(user, field, value)
    db.add(user)
    db.commit()
    db.refresh(user)
    # Changing residence can immediately complete continent achievements (the
    # home continent counts as visited), so re-evaluate badges right away.
    if home_changed:
        from app.workers.badge_worker import evaluate_badges_sync
        evaluate_badges_sync(str(user.id))
    return _profile(user, include_email=True)


@router.put("/me/featured", response_model=UserProfile)
def set_featured_badges(
    body: FeaturedBadgesUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Pin up to 3 earned badges to the profile. Order is preserved; ids that
    aren't earned (or duplicates) are dropped, capped at 3."""
    earned = set(
        db.execute(
            select(UserBadge.badge_id).where(UserBadge.user_id == user.id)
        ).scalars().all()
    )
    cleaned: list[str] = []
    for bid in body.badge_ids:
        if bid in earned and bid not in cleaned:
            cleaned.append(bid)
        if len(cleaned) == 3:
            break
    user.featured_badges = cleaned
    db.add(user)
    db.commit()
    db.refresh(user)
    return _profile(user, include_email=True)


# ---- taste profile (onboarding survey) ----

def _taste_out(row: TasteProfile | None) -> TasteProfileOut:
    answers = (row.answers if row else None) or {}
    done = bool(row and row.completed_at)
    return TasteProfileOut(
        answers=answers, completed=done,
        missing=[q for q in taste_profile.REQUIRED if q not in answers],
        archetype=taste_profile.archetype(answers) if done else None,
        summary=taste_profile.summary(answers),
        defaults=taste_profile.defaults_for(answers),
    )


@router.get("/me/profile/questions", response_model=SurveyOut)
def taste_questions():
    return SurveyOut(version=taste_profile.SURVEY_VERSION, questions=taste_profile.QUESTIONS)


@router.get("/me/profile", response_model=TasteProfileOut)
def get_taste_profile(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return _taste_out(db.get(TasteProfile, user.id))


@router.put("/me/profile", response_model=TasteProfileOut)
def save_taste_profile(body: TasteProfileUpdate, user: User = Depends(get_current_user),
                       db: Session = Depends(get_db)):
    """Save survey answers. Merges by default so the survey can autosave one
    answer at a time and resume later; completion is stamped once every required
    question is answered."""
    row = db.get(TasteProfile, user.id) or TasteProfile(user_id=user.id, answers={})
    incoming = taste_profile.validate(body.answers)
    answers = incoming if body.replace else {**(row.answers or {}), **incoming}
    row.answers = answers
    row.survey_version = taste_profile.SURVEY_VERSION
    if taste_profile.is_complete(answers):
        row.completed_at = row.completed_at or datetime.now(timezone.utc)
    else:
        row.completed_at = None
    db.add(row)
    db.commit()
    db.refresh(row)
    return _taste_out(row)


@router.get("/search", response_model=list[UserPublic])
def search_users(q: str = Query(min_length=1), db: Session = Depends(get_db)):
    rows = db.execute(
        select(User).where(User.username.ilike(f"%{q}%")).limit(20)
    ).scalars().all()
    return [
        UserPublic(id=str(u.id), username=u.username, display_name=u.display_name,
                   avatar_url=u.avatar_url, bio=u.bio)
        for u in rows
    ]


@router.get("/{username}", response_model=UserProfile)
def get_user(username: str, db: Session = Depends(get_db)):
    return _profile(_get_by_username(db, username))


@router.get("/{username}/badges", response_model=list[BadgeOut])
def get_user_badges(username: str, db: Session = Depends(get_db)):
    """Public: a user's badge showcase (all badges with their earned state)."""
    user = _get_by_username(db, username)
    earned = {
        ub.badge_id: ub
        for ub in db.execute(select(UserBadge).where(UserBadge.user_id == user.id)).scalars().all()
    }
    badges = db.execute(select(Badge)).scalars().all()
    return [
        BadgeOut(id=b.id, name=b.name, description=b.description, icon_url=b.icon_url,
                 emoji=b.emoji, category=b.category, requirement=b.requirement,
                 earned=b.id in earned,
                 earned_at=earned[b.id].earned_at if b.id in earned else None)
        for b in badges
    ]


@router.get("/{username}/stats", response_model=UserStats)
def get_stats(username: str, db: Session = Depends(get_db)):
    user = _get_by_username(db, username)
    return UserStats(**detailed_stats(db, user))


@router.get("/{username}/map", response_model=UserMap)
def get_map(username: str, db: Session = Depends(get_db)):
    user = _get_by_username(db, username)
    countries = db.execute(
        select(VisitedCountry).where(VisitedCountry.user_id == user.id)
    ).scalars().all()
    cities = db.execute(
        select(VisitedCity).where(VisitedCity.user_id == user.id)
    ).scalars().all()
    return UserMap(
        countries=[
            MapCountry(code=c.country_code, name=c.country_name,
                       first_visited=c.first_visited, visits=c.visit_count)
            for c in countries
        ],
        cities=[
            MapCity(name=c.city_name, country_code=c.country_code,
                    lat=c.lat, lng=c.lng, visits=c.visit_count)
            for c in cities
        ],
    )
