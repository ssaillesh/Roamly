"""Nightlife events Roamly lists itself: ones an admin adds (a special-guest night,
a St. Patrick's party) and ones clubs/promoters submit. Nothing shows in the app
until an admin approves it (status "approved")."""
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

EVENT_KINDS = ("club_night", "special_guest", "party", "holiday", "live_music", "comedy")
STATUSES = ("pending", "approved", "rejected")


class NightlifeEvent(Base):
    __tablename__ = "nightlife_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    venue_name: Mapped[str] = mapped_column(String(160), nullable=False)
    venue_id: Mapped[str | None] = mapped_column(String(80), nullable=True)     # a stored venue, if linked
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    address: Mapped[str | None] = mapped_column(String(300), nullable=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    price_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    free: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="club_night")
    special_day: Mapped[str | None] = mapped_column(String(30), nullable=True)  # special_days key
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="pending", index=True)
    source: Mapped[str] = mapped_column(String(12), nullable=False, default="admin")   # admin | submission
    submitter_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    submitter_email: Mapped[str | None] = mapped_column(String(254), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
