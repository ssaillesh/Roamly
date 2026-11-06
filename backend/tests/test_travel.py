"""Getting between stops: free estimates by distance and way of getting around."""
from app.services import travel

A = (43.6487, -79.3960)
NEAR = (43.6520, -79.3960)      # ~0.37 km north
FAR = (43.6900, -79.3960)       # ~4.6 km north


def test_short_hops_are_walked_whatever_the_plan_says():
    for mode in ("any", "transit", "walk"):
        assert travel.leg(A, NEAR, mode)["mode"] == "walk"
    assert travel.leg(A, NEAR, "walk")["minutes"] == round(0.37 * 1.25 / 4.8 * 60)


def test_longer_hops_by_transit_or_car():
    t, c = travel.leg(A, FAR, "transit"), travel.leg(A, FAR, "car")
    assert t["mode"] == "transit" and c["mode"] == "car"
    assert c["minutes"] < t["minutes"] < travel.leg(A, FAR, "walk")["minutes"]   # walking it is slowest
    assert t["km"] == round(travel.haversine_km(*A, *FAR) * travel.STREET_FACTOR, 2)


def test_farther_is_never_quicker():
    pts = [(A[0] + d / 111, A[1]) for d in (0.2, 0.5, 1, 2, 4, 8)]
    for mode in ("walk", "transit", "car", "any"):
        mins = [travel.leg(A, p, mode)["minutes"] for p in pts]
        # (switching from walking to transit at 1.6 km may add the wait, never take time off overall)
        assert mins[-1] > mins[0]


def test_a_real_router_can_be_swapped_in(monkeypatch):
    monkeypatch.setattr(travel, "ROUTER", lambda a, b, mode="any": {"km": 9.9, "minutes": 42, "mode": "car"})
    assert travel.legs([{"lat": A[0], "lng": A[1]}, {"lat": FAR[0], "lng": FAR[1]}], "car") == \
        [{"km": 9.9, "minutes": 42, "mode": "car"}]
