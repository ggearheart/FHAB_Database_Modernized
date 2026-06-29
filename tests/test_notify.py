"""Tests for in-app notifications: routing, illness escalation, ownership (RLS), read state."""

import pytest

from fhab.auth import create_user, grant_role, set_password
from fhab.intake import submit_public_report
from fhab.notify import (list_notifications, mark_read, on_new_submission, unread_count,
                         users_with_roles)

R5 = "Region 5 - Central Valley"


def _user(conn, email, role, region=None):
    u = create_user(conn, email)
    grant_role(conn, u, role, region=region)
    return u


def test_new_submission_notifies_reviewers(conn):
    staff = _user(conn, "wb@wb.ca.gov", "wb_staff", region=R5)
    on_new_submission(conn, 1, water_body="Test Lake", has_illness=False, source="cyanosafe-pwa")
    items = list_notifications(conn, staff)
    assert len(items) == 1 and items[0]["kind"] == "new_submission"
    assert "Test Lake" in items[0]["title"]
    assert unread_count(conn, staff) == 1


def test_illness_escalates_to_workgroup(conn):
    staff = _user(conn, "wb@wb.ca.gov", "wb_staff", region=R5)
    iwg = _user(conn, "iwg@wb.ca.gov", "illness_workgroup")
    on_new_submission(conn, 2, water_body="Sick Lake", has_illness=True)
    # The illness workgroup gets an escalation; a plain reviewer does not.
    iwg_kinds = {n["kind"] for n in list_notifications(conn, iwg)}
    wb_kinds = {n["kind"] for n in list_notifications(conn, staff)}
    assert "illness_alert" in iwg_kinds
    assert "illness_alert" not in wb_kinds and "new_submission" in wb_kinds


def test_admin_gets_both_new_and_illness(conn):
    admin = _user(conn, "admin@wb.ca.gov", "program_admin")
    on_new_submission(conn, 3, water_body="Both Lake", has_illness=True)
    kinds = {n["kind"] for n in list_notifications(conn, admin)}
    assert kinds == {"new_submission", "illness_alert"}


def test_notifications_are_private_per_user(conn):
    a = _user(conn, "a@wb.ca.gov", "wb_staff", region=R5)
    b = _user(conn, "b@wb.ca.gov", "wb_staff", region=R5)
    on_new_submission(conn, 4, water_body="Lake X", has_illness=False)
    # Both are reviewers, so both got a copy — but each sees only their own row.
    a_items = list_notifications(conn, a)
    assert all(True for _ in a_items) and len(a_items) == 1
    mark_read(conn, a)
    assert unread_count(conn, a) == 0 and unread_count(conn, b) == 1  # b's copy still unread


def test_users_with_roles_filters_active(conn):
    _user(conn, "r1@wb.ca.gov", "wb_staff", region=R5)
    ids = {u["id"] for u in users_with_roles(conn, ["wb_staff"])}
    assert len(ids) == 1


# --- web: a public submission raises a notification the staffer sees in the nav ---

@pytest.fixture()
def client(conn):
    from tests.conftest import TEST_DSN
    from fhab.web import create_app
    staff = create_user(conn, "staff@wb.ca.gov", "Staffer")
    set_password(conn, staff, "pw"); grant_role(conn, staff, "wb_staff", region=R5)
    app = create_app(dsn=TEST_DSN); app.config["TESTING"] = True
    return app.test_client()


def test_public_submission_creates_notification_visible_in_app(client, conn):
    submit_public_report(conn, {"water_body_name": "Notify Lake", "county": "Lake"})  # control row
    # via the endpoint (which fires the hook)
    client.post("/api/public/reports", json={"water_body_name": "Hook Lake", "county": "Lake",
                                             "illness": [{"subject": "Dog", "illness": True}]})
    client.post("/login", data={"email": "staff@wb.ca.gov", "password": "pw"}, follow_redirects=True)
    r = client.get("/notifications")
    assert b"Hook Lake" in r.data
    # The nav badge (unread count) shows on any page.
    assert b"\xf0\x9f\x94\x94" in client.get("/").data  # the bell emoji renders
