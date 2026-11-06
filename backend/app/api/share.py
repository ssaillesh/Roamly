"""Share the night: a plan gets a public link (/n/<code> on the web app) and a
story-sized card image. Creating a link needs an account; reading one doesn't."""
import hashlib
import json
import secrets
import string
from urllib.parse import urlparse

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Path, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.middleware.auth import get_current_user
from app.models import SharedPlan, User
from app.schemas.share import SharedPlanIn, SharedPlanOut, ShareLinkOut
from app.services import feedback
from app.services.share_card import card_for

router = APIRouter(prefix="/share", tags=["share"])

_ALPHABET = string.ascii_lowercase + string.digits
CODE = Path(pattern=r"^[a-z0-9]{6,16}$")


def _new_code(db: Session) -> str:
    for _ in range(8):
        code = "".join(secrets.choice(_ALPHABET) for _ in range(8))   # 36^8 ≈ 2.8e12
        if not db.scalar(select(SharedPlan.id).where(SharedPlan.code == code)):
            return code
    raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Couldn't make a link just now — try again.")


def share_url(code: str) -> str:
    return f"{settings.frontend_base_url.rstrip('/')}/n/{code}"


def _link_text(code: str) -> str:
    """'rova.app/n/k3x9a2qp' — the short form printed on the card."""
    host = urlparse(settings.frontend_base_url).netloc or settings.frontend_base_url
    return f"{host.removeprefix('www.')}/n/{code}"


def _get(db: Session, code: str) -> SharedPlan:
    row = db.scalar(select(SharedPlan).where(SharedPlan.code == code))
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This link doesn't go to a plan any more.")
    return row


@router.post("/plan", response_model=ShareLinkOut)
def share_plan(body: SharedPlanIn, background: BackgroundTasks, user: User = Depends(get_current_user),
               db: Session = Depends(get_db)):
    plan = body.model_dump(exclude_none=True)
    if feedback.counts_for(user) and (rows := feedback.rows_for("shared", plan["stops"])):
        background.add_task(feedback.record, user.id, feedback.plan_key(plan["stops"]), rows)
    digest = hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    row = db.scalar(select(SharedPlan).where(SharedPlan.user_id == user.id, SharedPlan.digest == digest))
    if not row:   # the same plan shared twice keeps one link
        row = SharedPlan(code=_new_code(db), user_id=user.id, digest=digest, title=plan["title"], plan=plan)
        db.add(row)
        db.commit()
    return ShareLinkOut(code=row.code, url=share_url(row.code), image_path=f"/share/plan/{row.code}/card.jpg")


@router.get("/plan/{code}", response_model=SharedPlanOut)
def get_shared_plan(code: str = CODE, db: Session = Depends(get_db)):
    row = _get(db, code)
    return SharedPlanOut(code=row.code, title=row.title, plan=row.plan, created_at=row.created_at)


@router.get("/plan/{code}/card.jpg")
async def shared_plan_card(code: str = CODE, db: Session = Depends(get_db)):
    row = _get(db, code)
    # Drawing fetches photos: keep it off the event loop.
    data = await run_in_threadpool(card_for, row.code, row.plan, _link_text(row.code))
    return Response(data, media_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=86400, immutable",
                             "Content-Disposition": f'inline; filename="rova-{row.code}.jpg"'})
