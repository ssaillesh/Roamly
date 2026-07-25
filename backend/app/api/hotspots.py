"""Hotspot feed — categorised POIs (party / nature / history) for Canadian cities,
sourced free from OpenStreetMap Overpass. Public (no auth), like the map endpoints.
"""
from fastapi import APIRouter, HTTPException, status, Query

from app.schemas.hotspots import (
    Hotspot, HotspotCity, HotspotCategory, HotspotMeta, HotspotFeed,
)
from app.services.hotspots import CANADA_CITIES, CATEGORIES, fetch_hotspots

router = APIRouter(prefix="/hotspots", tags=["hotspots"])


@router.get("/meta", response_model=HotspotMeta)
def meta():
    """Available cities + categories, so the client can build its controls."""
    return HotspotMeta(
        cities=[HotspotCity(key=k, label=v["label"], lat=v["lat"], lng=v["lng"])
                for k, v in CANADA_CITIES.items()],
        categories=[HotspotCategory(key=k, label=v["label"], icon=v["icon"])
                    for k, v in CATEGORIES.items()],
    )


@router.get("", response_model=HotspotFeed)
def get_hotspots(
    category: str = Query(..., description="food | activities | party | nature | history | shops"),
    city: str | None = Query(None, description="City key, e.g. 'toronto'"),
    lat: float | None = Query(None, description="Latitude for an exact-location search"),
    lng: float | None = Query(None, description="Longitude for an exact-location search"),
    radius: int = Query(3000, ge=300, le=3000, description="Search radius in metres (lat/lng mode), 3km max"),
):
    category = category.lower()
    if category not in CATEGORIES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown category")

    if lat is not None and lng is not None:
        spots = [Hotspot(**h) for h in fetch_hotspots(category, lat=lat, lng=lng, radius=radius)]
        return HotspotFeed(city="nearby", category=category, count=len(spots), hotspots=spots)

    city = (city or "").lower()
    if city not in CANADA_CITIES:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Provide a known city or lat/lng")
    spots = [Hotspot(**h) for h in fetch_hotspots(category, city_key=city)]
    return HotspotFeed(city=city, category=category, count=len(spots), hotspots=spots)
