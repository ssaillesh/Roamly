"""Plans lean towards places with a real photo of themselves — but only on near-ties."""
import pytest

from app.services import planner

A = (43.6487, -79.3960)


def _c(name, rating=4.4, image=None, d_lat=0.002):
    return {"name": name, "lat": A[0] + d_lat, "lng": A[1], "rating": rating, "review_count": 120,
            "price": "$$", "categories": ["restaurant"], "image": image}


def test_on_a_near_tie_the_place_with_a_photo_is_picked():
    bare, photo = _c("Bare"), _c("Pictured", image="https://pictured.example/room.jpg")
    assert planner._score(photo, A, 1) - planner._score(bare, A, 1) == pytest.approx(planner.PHOTO_BONUS)
    assert planner._pick([bare, photo], set(), A, 1)["name"] == "Pictured"


def test_a_clearly_better_place_still_wins_without_a_photo():
    best = _c("Best", rating=4.9)                                       # half a star better beats the photo
    pictured = _c("Pictured", rating=4.4, image="https://pictured.example/room.jpg")
    assert planner._pick([pictured, best], set(), A, 1)["name"] == "Best"
    near = _c("Near")                                                   # close by…
    far = _c("Far", image="https://far.example/room.jpg", d_lat=0.02)   # …beats a photo 2 km away
    assert planner._pick([far, near], set(), A, 1)["name"] == "Near"
