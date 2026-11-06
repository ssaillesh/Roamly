"""Getting from one stop to the next: how far, how long, and how.

Free estimates, no routing service: the straight-line distance times a street factor,
at a typical speed for each way of getting around, plus the waiting a trip involves
(a streetcar, finding parking). Good enough to time a night and order its stops;
not turn-by-turn. A short hop is walked whatever the plan's transport.

Everything asks `leg()`. To use a real router later (OSRM, Valhalla, Google), point
`ROUTER` at a function with the same signature; callers don't change.
"""
import math

# Streets aren't straight: Toronto's grid makes a trip about a quarter longer than the
# crow flies. webui/itinerary.js uses the same factor for its own fallback.
STREET_FACTOR = 1.25
SPEED_KMH = {"walk": 4.8, "transit": 18.0, "car": 25.0}   # downtown, door to door
WAIT_MIN = {"walk": 0, "transit": 6, "car": 4}             # the streetcar; parking
# Under this, you walk it (webui/itinerary.js walks under 1.6 km too); drivers only
# for a hop round the corner.
WALK_UNDER_KM = {"any": 1.6, "transit": 1.6, "car": 0.5, "walk": float("inf")}


def haversine_km(a_lat: float, a_lng: float, b_lat: float, b_lng: float) -> float:
    dlat, dlng = math.radians(b_lat - a_lat), math.radians(b_lng - a_lng)
    x = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(a_lat)) * math.cos(math.radians(b_lat)) * math.sin(dlng / 2) ** 2)
    return 2 * 6371 * math.asin(math.sqrt(x))


def estimate_leg(a: tuple[float, float], b: tuple[float, float], mode: str = "any") -> dict:
    """{"km": street distance, "minutes": door to door, "mode": "walk" | "transit" | "car"}."""
    km = haversine_km(a[0], a[1], b[0], b[1]) * STREET_FACTOR
    plan_mode = mode if mode in WALK_UNDER_KM else "any"
    how = "walk" if km < WALK_UNDER_KM[plan_mode] else ("car" if plan_mode == "car" else "transit")
    minutes = km / SPEED_KMH[how] * 60 + WAIT_MIN[how]
    return {"km": round(km, 2), "minutes": max(1, round(minutes)), "mode": how}


# The swap point for a real router: same arguments, same result shape.
ROUTER = estimate_leg


def leg(a: tuple[float, float], b: tuple[float, float], mode: str = "any") -> dict:
    return ROUTER(a, b, mode)


def legs(stops: list[dict], mode: str = "any") -> list[dict]:
    """One leg between each pair of consecutive stops."""
    return [leg((x["lat"], x["lng"]), (y["lat"], y["lng"]), mode) for x, y in zip(stops, stops[1:])]
