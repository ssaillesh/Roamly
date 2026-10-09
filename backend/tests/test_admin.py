"""Admin dashboard: access control, what gets recorded, cleanup, exports."""
import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import delete, select

from app.database import SessionLocal
from app.middleware import request_stats
from app.models import DailyRollup, PlanLog, User, UserActivityDay
from app.services import analytics, usage
from tests.conftest import auth_headers, make_user

A = "/api/v1/admin"
READ_ROUTES = ["/overview", "/activity", "/funnel", "/traffic", "/geo", "/planner", "/taste",
               "/health", "/apis", "/users", "/waitlist", "/audit"]


def _set(user_id, **fields):
    with SessionLocal() as db:
        u = db.get(User, uuid.UUID(user_id))
        for k, v in fields.items():
            setattr(u, k, v)
        db.commit()


def make_admin(client):
    user, token = make_user(client, "admin")
    _set(user["id"], is_admin=True)
    return user, token


def make_real_user(client):
    """An account that counts in the stats (not an @example.com test account)."""
    uid = uuid.uuid4().hex[:8]
    r = client.post("/api/v1/auth/register", json={
        "email": f"real_{uid}@roamly-tests.dev", "username": f"real_{uid}",
        "display_name": "Real", "password": "password123"})
    assert r.status_code == 201, r.text
    return r.json()["user"], r.json()["access_token"]


def test_non_admins_are_kept_out(client):
    _, token = make_user(client, "plain")
    for route in READ_ROUTES:
        assert client.get(A + route).status_code == 401, route
        assert client.get(A + route, headers=auth_headers(token)).status_code == 403, route
    assert client.get(A + "/export/users.csv", headers=auth_headers(token)).status_code == 403
    assert client.get("/metrics", headers=auth_headers(token)).status_code == 403
    assert client.get("/metrics").status_code == 401
    assert client.get("/api/v1/plan/debug", headers=auth_headers(token)).status_code == 403


def test_admin_sees_every_section(client):
    _, token = make_admin(client)
    for route in READ_ROUTES:
        r = client.get(A + route, headers=auth_headers(token))
        assert r.status_code == 200, (route, r.text)
    assert client.get("/metrics", headers=auth_headers(token)).status_code == 200
    me = client.get("/api/v1/users/me", headers=auth_headers(token)).json()
    assert me["is_admin"] is True
    # GA4 isn't configured in tests: the section says so instead of failing.
    web = client.get(A + "/traffic", headers=auth_headers(token)).json()["web"]
    assert web["connected"] is False


def test_test_accounts_left_out_of_stats(client):
    _, token = make_admin(client)
    h = auth_headers(token)

    def totals():
        return (client.get(A + "/overview", headers=h).json()["users_total"],
                client.get(A + "/overview?include_test=true", headers=h).json()["users_total"])

    real0, all0 = totals()
    make_user(client, "smoke")                      # @example.com → is_test
    real1, all1 = totals()
    assert (real1, all1) == (real0, all0 + 1)
    real_user, _ = make_real_user(client)
    real2, all2 = totals()
    assert (real2, all2) == (real1 + 1, all1 + 1)

    # Clean up through the admin API (exercises delete + the audit log).
    assert client.delete(f"{A}/users/{real_user['id']}", headers=h).status_code == 204
    audit = client.get(A + "/audit", headers=h).json()
    assert audit[0]["action"] == "delete_user"


def test_disabled_account_is_locked_out(client):
    _, admin_token = make_admin(client)
    user, token = make_user(client, "dis")
    r = client.post(f"{A}/users/{user['id']}/disabled", json={"value": True},
                    headers=auth_headers(admin_token))
    assert r.status_code == 200 and r.json()["is_disabled"] is True
    assert client.get("/api/v1/users/me", headers=auth_headers(token)).status_code == 403
    login = client.post("/api/v1/auth/login",
                        json={"email": f"{user['username']}@example.com", "password": "password123"})
    assert login.status_code == 403
    client.post(f"{A}/users/{user['id']}/disabled", json={"value": False},
                headers=auth_headers(admin_token))
    assert client.get("/api/v1/users/me", headers=auth_headers(token)).status_code == 200


def test_admin_cannot_disable_or_delete_self(client):
    admin, token = make_admin(client)
    h = auth_headers(token)
    assert client.post(f"{A}/users/{admin['id']}/disabled", json={"value": True}, headers=h).status_code == 400
    assert client.delete(f"{A}/users/{admin['id']}", headers=h).status_code == 400


def test_activity_recorded_once_per_day(client):
    user, token = make_user(client, "act")
    for _ in range(3):
        client.get("/api/v1/users/me", headers=auth_headers(token))
    with SessionLocal() as db:
        days = db.scalars(select(UserActivityDay.day)
                          .where(UserActivityDay.user_id == uuid.UUID(user["id"]))).all()
        assert days == [analytics.utc_today()]
        assert db.get(User, uuid.UUID(user["id"])).last_seen_at is not None


def test_plan_request_is_logged(client):
    user, token = make_user(client, "plog")
    # No location → the planner asks where; it's still logged, but not as "found nothing".
    r = client.post("/api/v1/plan/chat", headers=auth_headers(token),
                    json={"messages": [{"role": "user", "content": "something fun tonight"}]})
    assert r.status_code == 200
    with SessionLocal() as db:
        logs = db.scalars(select(PlanLog).where(PlanLog.user_id == uuid.UUID(user["id"]))).all()
    assert len(logs) == 1
    log = logs[0]
    assert log.endpoint == "chat" and log.user_text == "something fun tonight"
    assert log.found_nothing is False and log.response["type"] == r.json()["type"]


def test_cleanup_rolls_up_then_deletes_old_rows(client):
    user, _ = make_real_user(client)
    uid = uuid.UUID(user["id"])
    old_day = date(2001, 2, 3)
    old = datetime(2001, 2, 3, 12, tzinfo=timezone.utc)
    with SessionLocal() as db:
        db.add_all([PlanLog(user_id=uid, endpoint="chat", created_at=old, stops=0, found_nothing=True,
                            area="Testville, CA", prefs={"vibe": "chill"}, duration_ms=100),
                    PlanLog(user_id=uid, endpoint="build", created_at=old, stops=4, duration_ms=300),
                    PlanLog(user_id=uid, endpoint="chat", stops=3, duration_ms=200)])  # today: kept
        db.commit()
        try:
            analytics.daily_maintenance(db)
            left = db.scalars(select(PlanLog.created_at).where(PlanLog.user_id == uid)).all()
            assert len(left) == 1 and left[0] > datetime.now(timezone.utc) - timedelta(days=1)
            rolled = {(m, k): v for m, k, v in db.execute(
                select(DailyRollup.metric, DailyRollup.key, DailyRollup.value)
                .where(DailyRollup.day == old_day))}
            assert rolled[("plans", "chat")] == 1 and rolled[("plans", "build")] == 1
            assert rolled[("plans_nothing", "")] == 1
            assert rolled[("area", "Testville, CA")] == 1 and rolled[("vibe", "chill")] == 1
        finally:
            db.execute(delete(DailyRollup).where(DailyRollup.day == old_day))
            db.execute(delete(User).where(User.id == uid))
            db.commit()


def test_csv_export(client):
    _, token = make_admin(client)
    r = client.get(A + "/export/users.csv", headers=auth_headers(token))
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert r.text.splitlines()[0].startswith("id,email,username")
    assert client.get(A + "/export/nope.csv", headers=auth_headers(token)).status_code == 404


def test_request_stats_group_by_route_template(client):
    _, token = make_admin(client)
    request_stats.drain()
    client.get(f"{A}/users/{uuid.uuid4()}", headers=auth_headers(token))   # 404 from the handler
    client.get("/definitely-not-a-route")
    stats = request_stats.drain()
    assert stats["GET /api/v1/admin/users/{user_id}"][1] == 1      # one 4xx
    assert "unmatched" in stats


def test_usage_counter_counts_errors():
    usage.drain()
    usage.record("yelp", ok=True)
    try:
        with usage.counted("yelp"):
            raise ValueError("boom")
    except ValueError:
        pass
    counts = usage.drain()
    assert counts[(analytics.utc_today(), "yelp")] == [2, 1]
