import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, SmallInteger, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class PlanReview(Base):
    """A person's 1–5 star rating of a plan Rova made them, with an optional comment.
    One per person per plan (rating the same plan again updates it). A copy of the plan
    is kept so the admin page can show exactly what was rated. Deleted with the account."""
    __tablename__ = "plan_reviews"
    __table_args__ = (UniqueConstraint("user_id", "plan_key", name="uq_plan_review_once"),
                      CheckConstraint("stars BETWEEN 1 AND 5", name="ck_plan_review_stars"))

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    plan_key: Mapped[str] = mapped_column(String(32), nullable=False)      # services/feedback.plan_key
    stars: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    plan: Mapped[dict] = mapped_column(JSONB, nullable=False)              # {"plan": …} or {"days": […]}
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)   # an admin read it
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
