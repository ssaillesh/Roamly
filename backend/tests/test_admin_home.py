"""The admin overview (GET /admin/home), the review queue (GET /admin/queue) and the
Users page's extra numbers and detail panel."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.database import SessionLocal
from app.models import NightlifeEvent, PlanLog
from tests.conftest import auth_headers, make_user
from tests.test_admin import make_admin, make_real_user

A = (43.65, -79.38)


def _plan(name="Bar Raval"):
    return {"vibe": "chill", "budget": 150, "party_size": 2, "estimated_cost": 80, "start": "2026-11-06T21:00-05:00",
            "center": {"lat": A[0], "lng": A[1]},
            "stops": [{"slot": "dinner", "label": "Dinner", "icon": "x", "name": name, "lat": A[0], "lng": A[1],
                       "est_cost": 20, "time": "9:00 PM", "image": "https://img.example/raval.jpg"},
                      {"slot": "leisure", "label": "Live music", "icon": "x", "name": "The Rex",
                       "lat": A[0], "lng": A[1], "est_cost": 20, "time": "10:30 PM"}]}


@pytest.fixture
def pending_event():
    tag = uuid.uuid4().hex[:6]
    with SessionLocal() as db:
        e = NightlifeEvent(title=f"Rooftop vinyl {tag}", venue_name="Lavelle", kind="club_night",
                           starts_at=datetime.now(timezone.utc) + timedelta(days=2), price_min=20,
                           status="pending", source="submission", submitter_name="Lavelle team")
        db.add(e)
        db.commit()
        eid = e.id
    yield eid
    with SessionLocal() as db:
        db.delete(db.get(NightlifeEvent, eid))
        db.commit()


def _log_plan(user_id, plan):
    with SessionLocal() as db:
        db.add(PlanLog(user_id=uuid.UUID(user_id), endpoint="chat", response_type="itinerary", stops=len(plan["stops"]),
                       llm_calls=0, llm_failures=0, duration_ms=10,
                       response={"type": "itinerary", "message": "", "plan": plan}))
        db.commit()


def test_the_queue_holds_pending_submissions_and_unread_low_ratings(client, pending_event):
    _, admin = make_admin(client)
    _, token = make_real_user(client)
    rid = client.post("/api/v1/plan/review", headers=auth_headers(token),
                      json={"plan": _plan(), "stars": 1, "comment": "Nothing was open."}).json()["id"]
    happy = client.post("/api/v1/plan/review", headers=auth_headers(token),
                        json={"plan": _plan("Bar Isabel"), "stars": 5}).json()["id"]
    h = auth_headers(admin)
    q = client.get("/api/v1/admin/queue", headers=h).json()
    ids = {(i["kind"], i["id"]) for i in q["items"]}
    assert ("event", str(pending_event)) in ids and ("review", rid) in ids
    assert ("review", happy) not in ids                                  # good ratings aren't a job
    ev = next(i for i in q["items"] if i["id"] == str(pending_event))
    assert ev["from"] == "Lavelle team" and "Lavelle" in ev["sub"] and "$20" in ev["sub"]
    assert q["pending"] >= 2 and q["oldest"]

    # approving the event and reading the rating clears both from "pending"…
    client.post(f"/api/v1/admin/events/{pending_event}/status", headers=h, json={"status": "approved"})
    client.post(f"/api/v1/admin/reviews/{rid}/reviewed", headers=h, json={"value": True})
    ids = {i["id"] for i in client.get("/api/v1/admin/queue", headers=h).json()["items"]}
    assert str(pending_event) not in ids and rid not in ids
    # …and "all" still shows them, decided
    every = client.get("/api/v1/admin/queue?show=all", headers=h).json()["items"]
    done = next(i for i in every if i["id"] == str(pending_event))
    assert done["status"] == "approved" and not done["pending"]


def test_the_overview_counts_todays_plans(client):
    _, admin = make_admin(client)
    real, _ = make_real_user(client)
    before = client.get("/api/v1/admin/home", headers=auth_headers(admin)).json()
    _log_plan(real["id"], _plan())
    d = client.get("/api/v1/admin/home", headers=auth_headers(admin)).json()
    assert d["plans_today"] == before["plans_today"] + 1
    assert len(d["start_hours"]) == 24 and d["start_hours"][0]["hour"] == 6
    nine = next(b for b in d["start_hours"] if b["hour"] == 21)
    assert nine["count"] >= 1                                            # read from the plan's own start time
    assert any(m["name"] == "Bar Raval" for m in d["most_added"])
    assert {"venues", "events_tonight", "queue", "share_rate", "name"} <= d.keys()


def test_users_page_numbers_and_the_detail_panel(client):
    _, admin = make_admin(client)
    real, _ = make_real_user(client)
    _log_plan(real["id"], _plan())
    h = auth_headers(admin)
    d = client.get(f"/api/v1/admin/users?q={real['username']}&filter=real", headers=h).json()
    assert d["counts"]["real"] == 1 and d["counts"]["test"] == 0 and d["total"] == 1
    assert d["users"][0]["survey"] == "none" and d["users"][0]["plans"] == 1
    assert {"real", "active_week", "survey_rate", "planned_week_rate"} <= d["summary"].keys()
    u = client.get(f"/api/v1/admin/users/{real['id']}", headers=h).json()
    assert u["plans_total"] == 1 and u["shared_total"] == 0
    assert u["latest_plan"]["names"] == ["Bar Raval", "The Rex"] and u["latest_plan"]["each"] == 40


def test_only_admins_see_the_overview_and_queue(client):
    _, token = make_user(client, "nosy")
    for path in ("/api/v1/admin/home", "/api/v1/admin/queue"):
        assert client.get(path, headers=auth_headers(token)).status_code == 403
