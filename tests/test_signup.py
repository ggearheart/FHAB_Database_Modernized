"""Self-service signup (provisional, admin-reviewed) + statewide vs region-scoped staff."""

import pytest

from fhab.auth import (approve_signup, authenticate, create_user, grant_role, is_pending_signup,
                       list_roles_for, reject_signup, request_signup, set_password)


def test_signup_is_pending_and_cannot_sign_in(conn):
    uid = request_signup(conn, "New@Example.org", "New User", "password123", "WB staff, Region 5")
    assert uid
    # inactive + pending -> authenticate refuses it
    assert authenticate(conn, "new@example.org", "password123") is None
    assert is_pending_signup(conn, "new@example.org")
    # duplicate email is rejected
    assert request_signup(conn, "new@example.org", None, "otherpassword") is None


def test_approve_activates_and_grants(conn):
    uid = request_signup(conn, "app@example.org", "A", "password123")
    approve_signup(conn, uid)
    grant_role(conn, uid, "wb_staff", region="Region 5")
    assert authenticate(conn, "app@example.org", "password123")           # now active
    assert "wb_staff" in list_roles_for(conn, uid)
    assert not is_pending_signup(conn, "app@example.org")


def test_reject_deletes_pending(conn):
    uid = request_signup(conn, "rej@example.org", None, "password123")
    reject_signup(conn, uid)
    assert conn.execute("SELECT 1 FROM app_user WHERE id=%s", (uid,)).fetchone() is None


def test_statewide_vs_region_scope(conn):
    u = create_user(conn, "sw@wb.ca.gov")
    grant_role(conn, u, "wb_staff", region=None)      # statewide
    grant_role(conn, u, "wb_staff", region="Region 5")  # plus a specific region
    scopes = {r["scope_region"] for r in conn.execute(
        "SELECT scope_region FROM user_role WHERE user_id=%s AND role_code='wb_staff'", (u,)).fetchall()}
    assert None in scopes and "Region 5" in scopes


# --- web ---

@pytest.fixture()
def client(conn):
    from fhab.web import create_app
    from tests.conftest import TEST_DSN
    admin = create_user(conn, "admin@wb.ca.gov"); set_password(conn, admin, "pw")
    grant_role(conn, admin, "program_admin")
    app = create_app(dsn=TEST_DSN); app.config["TESTING"] = True
    return app.test_client()


def test_signup_then_admin_approve_statewide_web(client, conn):
    # public request via the login/signup page
    client.post("/signup", data={"email": "web@ex.org", "full_name": "Web", "password": "password123",
                                 "note": "Region 5 staff"}, follow_redirects=True)
    uid = conn.execute("SELECT id FROM app_user WHERE email='web@ex.org' AND signup_pending").fetchone()["id"]
    # a pending account can't sign in and gets the review message
    r = client.post("/login", data={"email": "web@ex.org", "password": "password123"}, follow_redirects=True)
    assert b"awaiting an administrator" in r.data

    client.post("/login", data={"email": "admin@wb.ca.gov", "password": "pw"}, follow_redirects=True)
    assert b"web@ex.org" in client.get("/admin/users").data
    # approve as statewide wb_staff (blank region = statewide)
    client.post(f"/admin/users/{uid}/approve", data={"role": "wb_staff", "region": ""}, follow_redirects=True)
    row = conn.execute("SELECT is_active, signup_pending FROM app_user WHERE id=%s", (uid,)).fetchone()
    assert row["is_active"] and not row["signup_pending"]
    sr = conn.execute("SELECT scope_region FROM user_role WHERE user_id=%s AND role_code='wb_staff'",
                      (uid,)).fetchone()
    assert sr["scope_region"] is None                # statewide
