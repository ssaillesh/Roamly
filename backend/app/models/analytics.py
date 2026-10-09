"""Tables behind the admin dashboard: activity, plan logs, server health,
outside-API usage, daily rollups and the admin audit log.

Raw rows (plan_logs, health_snapshots) are kept for RETENTION_DAYS; the daily
cleanup in services/analytics.py rolls them up into daily_rollups first, so the
charts keep their history after the raw rows are gone.
"""
import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, func, false,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class UserActivityDay(Base):
    """One row per user per day they used the app (DAU/WAU/MAU, retention)."""
    __tablename__ = "user_activity_days"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True, index=True)


class PlanLog(Base):
    """One planner request: what the user asked for and what they got back."""
    __tablename__ = "plan_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    endpoint: Mapped[str] = mapped_column(String(20), nullable=False)   # chat | options | build
    # One per conversation on the page, so a whole back-and-forth can be read together.
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # new_plan | edit_plan | events | ask_about_plan | chitchat (chat only)
    intent: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True)
    user_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    prefs: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    area: Mapped[str | None] = mapped_column(String(100), nullable=True)
    response_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    stops: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    found_nothing: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    llm_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    llm_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    response: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class HealthSnapshot(Base):
    """Request stats for one minute of one API process."""
    __tablename__ = "health_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    requests: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status_4xx: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status_5xx: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    p50_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    p95_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    # {route: {"n", "4xx", "5xx", "p50", "p95"}}
    routes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    cold_start: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())


class ApiUsage(Base):
    """Calls to one outside provider on one day (kept forever; it's tiny)."""
    __tablename__ = "api_usage"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    provider: Mapped[str] = mapped_column(String(40), primary_key=True)
    calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errors: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class DailyRollup(Base):
    """A daily total that outlives the raw rows it was counted from."""
    __tablename__ = "daily_rollups"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    metric: Mapped[str] = mapped_column(String(60), primary_key=True)
    key: Mapped[str] = mapped_column(String(100), primary_key=True, default="")
    value: Mapped[float] = mapped_column(Float, nullable=False, default=0)


class AdminAuditLog(Base):
    """Every action an admin takes (disable, delete, invite, export...)."""
    __tablename__ = "admin_audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    admin_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    target: Mapped[str | None] = mapped_column(String(255), nullable=True)
    details: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True)
