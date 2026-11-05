"""The stored venue list the planner picks from (loaded monthly by
scripts/load_venues.py from Overture Maps + Wikidata + OpenStreetMap), and the
neighbourhood names used to place "Kensington Market" without a live geocoder.

Plans read from here instead of calling a live map search, which is slow and
often down. A city that hasn't been loaded falls back to the live search.
"""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Venue(Base):
    __tablename__ = "venues"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)       # "ovt:<overture id>"
    city: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lng: Mapped[float] = mapped_column(Float, nullable=False)
    # Planner stops this place can be: ["dinner", "lunch"], ["dessert", "cafe"], …
    slots: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    category: Mapped[str] = mapped_column(String(80), nullable=False)    # "sushi restaurant"
    # The category and its parents, readable ("japanese restaurant", "restaurant")
    categories: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    chain: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    fast_food: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    website: Mapped[str | None] = mapped_column(String(500), nullable=True)
    address: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # OSM opening_hours syntax ("Mo-Fr 11:00-22:00; Sa-Su 10:00-23:00"); None = unknown
    hours: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # How well known: Wikipedia language editions covering it (Wikidata sitelinks); 0 = none
    notability: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    wikidata: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # A photo URL: the website's preview image (og:image), else the Wikimedia Commons
    # photo. "" = the loader looked and found none; None = not looked at yet.
    image: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (Index("ix_venues_lat_lng", "lat", "lng"),)


class Area(Base):
    """A neighbourhood / locality name with its centre point."""
    __tablename__ = "areas"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    city: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)       # neighborhood | locality | …
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lng: Mapped[float] = mapped_column(Float, nullable=False)
