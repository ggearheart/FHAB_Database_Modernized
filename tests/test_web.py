"""Tests for the staff web app: login, RBAC, and report entry through RLS."""

import pytest

from fhab.auth import create_user, grant_role, set_password
from tests.conftest import TEST_DSN

R5 = "Region 5 - Central Valley"
R1 = "Region 1 - North Coast"


@pytest.fixture()
def app_client(conn):
    """Reset DB (via conn), seed an admin + a region-5 staffer, return a test client."""
    flask = pytest.importorskip("flask")  # noqa: F841
    from fhab.web import create_app

    admin = create_user(conn, "admin@fhab.local", "Admin")
    set_password(conn, admin, "adminpw"); grant_role(conn, admin, "program_admin")
    staff = create_user(conn, "staff@wb.ca.gov", "Staffer")
    set_password(conn, staff, "staffpw"); grant_role(conn, staff, "wb_staff", region=R5)

    app = create_app(dsn=TEST_DSN)
    app.config["TESTING"] = True
    return app.test_client()


def _login(client, email, pw):
    return client.post("/login", data={"email": email, "password": pw}, follow_redirects=True)


def test_login_required_redirects(app_client):
    r = app_client.get("/")
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


def test_login_and_dashboard(app_client):
    r = _login(app_client, "staff@wb.ca.gov", "staffpw")
    assert r.status_code == 200
    assert b"Welcome" in r.data and b"wb_staff" in r.data


def test_bad_password_rejected(app_client):
    r = _login(app_client, "staff@wb.ca.gov", "wrong")
    assert b"Invalid email or password" in r.data


def test_non_admin_cannot_reach_accounts(app_client):
    _login(app_client, "staff@wb.ca.gov", "staffpw")
    r = app_client.get("/admin/users", follow_redirects=True)
    assert b"Administrator access required" in r.data


def test_admin_can_manage_accounts(app_client):
    _login(app_client, "admin@fhab.local", "adminpw")
    r = app_client.get("/admin/users")
    assert r.status_code == 200 and b"Create account" in r.data
    # Create a new staffer via the UI.
    r = app_client.post("/admin/users/new", data={
        "email": "newhire@wb.ca.gov", "full_name": "New Hire", "password": "pw",
        "role": "wb_staff", "region": R5}, follow_redirects=True)
    assert b"Account created" in r.data and b"newhire@wb.ca.gov" in r.data


def test_staff_enters_report_in_region(app_client, conn):
    _login(app_client, "staff@wb.ca.gov", "staffpw")
    r = app_client.post("/reports/new", data={
        "waterbody": "WebApp Pond", "region": R5, "county": "Sacramento",
        "lat": "38.58", "lon": "-121.49", "bloom_type": "cyanobacteria"},
        follow_redirects=True)
    assert b"Report entered" in r.data
    n = conn.execute("SELECT count(*) c FROM waterbody WHERE water_body_name='WebApp Pond'").fetchone()["c"]
    assert n == 1


def test_cross_region_requires_confirmation(app_client, conn):
    _login(app_client, "staff@wb.ca.gov", "staffpw")
    # Region-5 staffer files for Region 1 without confirming -> warning, not created.
    r = app_client.post("/reports/new", data={"waterbody": "XR Pond", "region": R1}, follow_redirects=True)
    assert b"different Regional Board" in r.data
    assert conn.execute("SELECT count(*) c FROM waterbody WHERE water_body_name='XR Pond'").fetchone()["c"] == 0
    # Now with confirmation -> created.
    r = app_client.post("/reports/new", data={"waterbody": "XR Pond", "region": R1, "confirm_cross": "1"},
                        follow_redirects=True)
    assert b"Report entered" in r.data
    assert conn.execute("SELECT count(*) c FROM waterbody WHERE water_body_name='XR Pond'").fetchone()["c"] == 1
