import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class StopFeedback(Base):
    """What one person did with one stored venue in one plan: it was shown, they removed
    it or swapped it for another, or the plan it was in was saved or shared. Only the
    venue id and the kind of action: no text, no location. Deleted with the account, and
    after a year (services/feedback.py)."""
    __tablename__ = "stop_feedback"
    __table_args__ = (UniqueConstraint("user_id", "vid", "event", "plan_key", name="uq_stop_feedback_once"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    vid: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    slot: Mapped[str | None] = mapped_column(String(20), nullable=True)
    event: Mapped[str] = mapped_column(String(12), nullable=False)   # shown | removed | swapped | saved | shared
    # Which plan it was in (a short digest), so the same action on the same plan counts once.
    plan_key: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class VenueSignal(Base):
    """The daily summary of StopFeedback per venue: how many people saw it, kept it
    (saved or shared a plan with it) or dropped it, and the score the planner uses."""
    __tablename__ = "venue_signals"

    vid: Mapped[str] = mapped_column(String(80), primary_key=True)
    users: Mapped[int] = mapped_column(Integer, nullable=False, default=0)       # distinct people who saw it
    positive: Mapped[int] = mapped_column(Integer, nullable=False, default=0)    # … who saved or shared it
    negative: Mapped[int] = mapped_column(Integer, nullable=False, default=0)    # … who removed or swapped it
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)     # 0..1, 0.5 = no lean
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
