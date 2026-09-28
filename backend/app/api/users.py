from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User, TasteProfile
from app.middleware.auth import get_current_user
from app.schemas.user import UserProfile, UserUpdate
from app.schemas.profile import SurveyOut, TasteProfileOut, TasteProfileUpdate
from app.services import taste_profile

router = APIRouter(prefix="/users", tags=["users"])


def _profile(user: User) -> UserProfile:
    return UserProfile(
        id=str(user.id), username=user.username, display_name=user.display_name,
        avatar_url=user.avatar_url, bio=user.bio, home_city=user.home_city, email=user.email,
    )


@router.get("/me", response_model=UserProfile)
def get_me(user: User = Depends(get_current_user)):
    return _profile(user)


@router.patch("/me", response_model=UserProfile)
def update_me(body: UserUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    data = body.model_dump(exclude_unset=True)
    if "email" in data and data["email"] and data["email"] != user.email:
        if db.scalar(select(User.id).where(User.email == data["email"])):
            raise HTTPException(status.HTTP_409_CONFLICT, "Email already in use")
    for field, value in data.items():
        setattr(user, field, value)
    db.add(user)
    db.commit()
    db.refresh(user)
    return _profile(user)


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
