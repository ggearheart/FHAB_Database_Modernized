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


def test_report_detail_page_and_add_result(app_client, conn):
    _login(app_client, "staff@wb.ca.gov", "staffpw")
    app_client.post("/reports/new", data={"waterbody": "Detail Pond", "region": R5},
                    follow_redirects=True)
    rid = conn.execute(
        "SELECT bloom_report_id FROM event e JOIN location l ON l.id=e.location_id "
        "JOIN waterbody w ON w.id=l.waterbody_id WHERE w.water_body_name='Detail Pond'"
    ).fetchone()["bloom_report_id"]

    # Detail page renders.
    r = app_client.get(f"/reports/{rid}")
    assert r.status_code == 200 and b"Field &amp; lab results" in r.data

    # Edit the report (field verification).
    r = app_client.post(f"/reports/{rid}/edit",
                        data={"bloom_type": "cyanobacteria", "determination_code": "confirmed_hab"},
                        follow_redirects=True)
    assert b"Report updated" in r.data

    # Add a lab result.
    analyte_id = conn.execute("SELECT id FROM analyte WHERE analyte='Anatoxin-a'").fetchone()["id"]
    r = app_client.post(f"/reports/{rid}/results",
                        data={"data_type": "Laboratory", "analyte_id": str(analyte_id),
                              "measurement_value": "3.2", "measurement_unit": "ug/L"},
                        follow_redirects=True)
    assert b"Result added" in r.data
    n = conn.execute(
        "SELECT count(*) c FROM result r JOIN sample s ON s.id=r.sample_id WHERE s.bloom_report_id=%s",
        (rid,)).fetchone()["c"]
    assert n == 1


def test_map_page_and_geojson(app_client, conn):
    _login(app_client, "staff@wb.ca.gov", "staffpw")
    # A report with a point in Region 5.
    app_client.post("/reports/new", data={"waterbody": "Map Pond", "region": R5,
                                          "lat": "38.6", "lon": "-121.5"}, follow_redirects=True)
    assert app_client.get("/map").status_code == 200

    import json
    fc = json.loads(app_client.get("/api/reports.geojson").data)
    assert fc["type"] == "FeatureCollection"
    names = {f["properties"]["water_body_name"] for f in fc["features"]}
    assert "Map Pond" in names
    feat = next(f for f in fc["features"] if f["properties"]["water_body_name"] == "Map Pond")
    assert feat["geometry"]["type"] == "Point"
    assert -125 < feat["geometry"]["coordinates"][0] < -110  # lon


def test_geojson_is_rls_filtered(app_client, conn):
    # A Region-1 report shouldn't appear for a Region-5 staffer (no published advisory).
    wb = conn.execute(
        "INSERT INTO waterbody (water_body_name, regional_water_board) VALUES ('R1 Secret', %s) RETURNING id",
        (R1,)).fetchone()["id"]
    loc = conn.execute(
        "INSERT INTO location (waterbody_id, geom) VALUES (%s, ST_SetSRID(ST_MakePoint(-124,41),4326)) RETURNING id",
        (wb,)).fetchone()["id"]
    conn.execute("INSERT INTO event (bloom_report_id, location_id) VALUES (88001, %s)", (loc,))
    conn.commit()
    _login(app_client, "staff@wb.ca.gov", "staffpw")
    import json
    fc = json.loads(app_client.get("/api/reports.geojson").data)
    assert "R1 Secret" not in {f["properties"]["water_body_name"] for f in fc["features"]}


def test_staff_sets_report_determination(app_client, conn):
    _login(app_client, "staff@wb.ca.gov", "staffpw")
    app_client.post("/reports/new", data={"waterbody": "Outcome Pond", "region": R5},
                    follow_redirects=True)
    rid = conn.execute(
        "SELECT bloom_report_id FROM event e JOIN location l ON l.id=e.location_id "
        "JOIN waterbody w ON w.id=l.waterbody_id WHERE w.water_body_name='Outcome Pond'"
    ).fetchone()["bloom_report_id"]
    r = app_client.post(f"/reports/{rid}/determination",
                        data={"determination_code": "non_hab_algae"}, follow_redirects=True)
    assert b"Outcome updated" in r.data
    code = conn.execute(
        "SELECT determination_code FROM event WHERE bloom_report_id=%s", (rid,)).fetchone()
    assert code["determination_code"] == "non_hab_algae"


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
