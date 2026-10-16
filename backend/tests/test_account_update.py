"""PATCH /users/me: a stolen session alone can't move the account's email (where
password resets go), and nothing outside the editable profile fields can be set."""
import pytest

from tests.conftest import auth_headers, make_user

ME = "/api/v1/users/me"


def _me(client, token):
    return client.get(ME, headers=auth_headers(token)).json()


def test_profile_fields_change_without_a_password(client):
    _, token = make_user(client, "upd")
    r = client.patch(ME, headers=auth_headers(token),
                     json={"display_name": "New Name", "bio": "hi", "home_city": "Toronto",
                           "avatar_url": "https://img.example.com/a.png"})
    assert r.status_code == 200, r.text
    me = _me(client, token)
    assert (me["display_name"], me["bio"], me["home_city"]) == ("New Name", "hi", "Toronto")


def test_sending_the_same_email_needs_no_password(client):
    user, token = make_user(client, "upd")
    email = _me(client, token)["email"]
    r = client.patch(ME, headers=auth_headers(token), json={"email": email, "bio": "same email"})
    assert r.status_code == 200, r.text
    assert _me(client, token)["bio"] == "same email"


@pytest.mark.parametrize("extra", [{}, {"current_password": ""}, {"current_password": None},
                                   {"current_password": "wrong-password"}])
def test_email_change_needs_the_current_password(client, extra):
    _, token = make_user(client, "upd")
    before = _me(client, token)
    r = client.patch(ME, headers=auth_headers(token),
                     json={"email": "taken_over@example.com", "display_name": "Hijacked", **extra})
    assert r.status_code == 401
    after = _me(client, token)
    assert after["email"] == before["email"]
    assert after["display_name"] == before["display_name"]     # the safe field didn't sneak through


def test_email_change_with_the_right_password(client):
    user, token = make_user(client, "upd")
    old = _me(client, token)["email"]
    new = f"moved_{user['username']}@example.com"
    r = client.patch(ME, headers=auth_headers(token),
                     json={"email": new, "current_password": "password123", "bio": "moved"})
    assert r.status_code == 200, r.text
    assert r.json()["email"] == new and r.json()["bio"] == "moved"
    assert client.post("/api/v1/auth/login", json={"email": new, "password": "password123"}).status_code == 200
    assert client.post("/api/v1/auth/login", json={"email": old, "password": "password123"}).status_code == 401


def test_cannot_probe_which_emails_exist_without_the_password(client):
    other, other_token = make_user(client, "upd")
    _, token = make_user(client, "upd")
    taken = _me(client, other_token)["email"]
    blind = client.patch(ME, headers=auth_headers(token), json={"email": taken})
    assert blind.status_code == 401                             # not 409
    known = client.patch(ME, headers=auth_headers(token), json={"email": taken, "current_password": "password123"})
    assert known.status_code == 409


@pytest.mark.parametrize("field, value", [
    ("is_admin", True), ("is_disabled", True), ("is_test", True), ("username", "admin"),
    ("id", "00000000-0000-0000-0000-000000000000"), ("password", "new-password"),
    ("password_hash", "$2b$12$forged"), ("terms_version", "x"), ("Email", "caps@example.com"),
    ("e_mail", "alt@example.com"),
])
def test_protected_and_unknown_fields_are_refused(client, field, value):
    _, token = make_user(client, "upd")
    before = _me(client, token)
    r = client.patch(ME, headers=auth_headers(token), json={field: value, "bio": "with a protected field"})
    assert r.status_code == 422
    after = _me(client, token)
    assert after == before and after["is_admin"] is False


@pytest.mark.parametrize("body", [{"email": None, "current_password": "password123"}, {"display_name": None}])
def test_null_for_a_required_field_is_a_validation_error(client, body):
    _, token = make_user(client, "upd")
    r = client.patch(ME, headers=auth_headers(token), json=body)
    assert r.status_code == 422


def test_responses_never_carry_passwords(client):
    user, token = make_user(client, "upd")
    r = client.patch(ME, headers=auth_headers(token),
                     json={"email": f"x_{user['username']}@example.com", "current_password": "password123"})
    assert r.status_code == 200
    assert not {"password", "password_hash", "current_password"} & set(r.json())
    assert "password123" not in r.text and "$2b$" not in r.text


def test_still_needs_a_session(client):
    assert client.patch(ME, json={"bio": "anon"}).status_code == 401
