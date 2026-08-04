"""Account management: reset password, force-change, deactivate, edit, delete, self-service."""

import pytest

from fhab.auth import (authenticate, change_own_password, create_user, delete_user, gen_password,
                       grant_role, reset_password, set_active, set_password, update_user,
                       user_references)


def test_reset_forces_change_then_authenticate_flags_it(conn):
    uid = create_user(conn, "u@wb.ca.gov")
    reset_password(conn, uid, "Temp-Pass-1")
    row = authenticate(conn, "u@wb.ca.gov", "Temp-Pass-1")
    assert row and row["must_change_password"] is True
    assert conn.execute("SELECT last_login_at FROM app_user WHERE id=%s",
                        (uid,)).fetchone()["last_login_at"] is not None   # sign-in recorded


def test_change_own_password_clears_flag(conn):
    uid = create_user(conn, "c@wb.ca.gov")
    reset_password(conn, uid, "Temp-1")
    assert change_own_password(conn, uid, "wrong", "NewPass-9") is False    # bad current
    assert change_own_password(conn, uid, "Temp-1", "NewPass-9") is True
    row = authenticate(conn, "c@wb.ca.gov", "NewPass-9")
    assert row and row["must_change_password"] is False


def test_deactivate_blocks_signin(conn):
    uid = create_user(conn, "d@wb.ca.gov")
    set_password(conn, uid, "pw12345678")
    assert authenticate(conn, "d@wb.ca.gov", "pw12345678")
    set_active(conn, uid, False)
    assert authenticate(conn, "d@wb.ca.gov", "pw12345678") is None
    set_active(conn, uid, True)
    assert authenticate(conn, "d@wb.ca.gov", "pw12345678")


def test_update_user_and_duplicate_email(conn):
    a = create_user(conn, "a@wb.ca.gov")
    create_user(conn, "b@wb.ca.gov")
    update_user(conn, a, full_name="Alice A", email="alice@wb.ca.gov")
    assert conn.execute("SELECT email, full_name FROM app_user WHERE id=%s", (a,)).fetchone()["email"] == "alice@wb.ca.gov"
    with pytest.raises(ValueError):
        update_user(conn, a, email="b@wb.ca.gov")                # taken


def test_delete_guarded_by_references(conn):
    keep = create_user(conn, "ref@wb.ca.gov")
    grant_role(conn, keep, "wb_staff", region="Region 5")       # a granted role is the user's own
    # a fresh user with a role grant BY someone else -> referenced via granted_by
    other = create_user(conn, "other@wb.ca.gov")
    conn.execute("UPDATE user_role SET granted_by=%s WHERE user_id=%s", (keep, keep)); conn.commit()
    assert user_references(conn, keep) >= 1                      # granted_by reference
    unused = create_user(conn, "unused@wb.ca.gov")
    assert user_references(conn, unused) == 0
    delete_user(conn, unused)
    assert conn.execute("SELECT 1 FROM app_user WHERE id=%s", (unused,)).fetchone() is None


def test_gen_password_is_unambiguous():
    pw = gen_password()
    assert len(pw) == 12 and not (set(pw) & set("0O1lI"))


# ---------- web: admin actions + self-service + gate ----------

@pytest.fixture()
def admin_client(conn):
    from fhab.web import create_app
    from tests.conftest import TEST_DSN
    a = create_user(conn, "admin@wb.ca.gov"); set_password(conn, a, "adminpass1")
    grant_role(conn, a, "program_admin")
    conn.commit()
    app = create_app(dsn=TEST_DSN); app.config["TESTING"] = True
    cl = app.test_client()
    cl.post("/login", data={"email": "admin@wb.ca.gov", "password": "adminpass1"}, follow_redirects=True)
    return cl


def test_admin_reset_shows_temp_password_and_gate_forces_change(admin_client, conn):
    uid = create_user(conn, "target@wb.ca.gov"); conn.commit()
    r = admin_client.post(f"/admin/users/{uid}/reset-password", data={"password": "Given-Temp-1"},
                          follow_redirects=True)
    assert "Temporary password: Given-Temp-1" in r.get_data(as_text=True)

    # the reset user logs in -> forced to the change-password page for any route
    from fhab.web import create_app
    from tests.conftest import TEST_DSN
    cl = create_app(dsn=TEST_DSN).test_client()
    cl.post("/login", data={"email": "target@wb.ca.gov", "password": "Given-Temp-1"}, follow_redirects=True)
    assert cl.get("/", follow_redirects=False).headers["Location"].endswith("/account/password")
    # after changing, the gate clears (dashboard is reachable to any logged-in user)
    cl.post("/account/password", data={"current": "Given-Temp-1", "new": "MyNewPass9",
                                       "confirm": "MyNewPass9"}, follow_redirects=True)
    assert cl.get("/", follow_redirects=False).status_code == 200


def test_admin_cannot_deactivate_or_delete_self(admin_client, conn):
    me = conn.execute("SELECT id FROM app_user WHERE email='admin@wb.ca.gov'").fetchone()["id"]
    admin_client.post(f"/admin/users/{me}/active", data={"active": "0"}, follow_redirects=True)
    assert conn.execute("SELECT is_active FROM app_user WHERE id=%s", (me,)).fetchone()["is_active"] is True
    admin_client.post(f"/admin/users/{me}/delete", follow_redirects=True)
    assert conn.execute("SELECT 1 FROM app_user WHERE id=%s", (me,)).fetchone() is not None
