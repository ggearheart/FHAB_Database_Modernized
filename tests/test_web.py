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


def test_upload_lab_results_for_event(app_client, conn):
    import io
    from pathlib import Path
    _login(app_client, "staff@wb.ca.gov", "staffpw")
    app_client.post("/reports/new", data={"waterbody": "Upload Lake", "region": R5}, follow_redirects=True)
    rid = conn.execute(
        "SELECT bloom_report_id FROM event e JOIN location l ON l.id=e.location_id "
        "JOIN waterbody w ON w.id=l.waterbody_id WHERE w.water_body_name='Upload Lake'"
    ).fetchone()["bloom_report_id"]

    chem = Path(__file__).parent / "fixtures" / "ceden" / "CEDEN_WaterChemistry.csv"
    data = {"chem_file": (io.BytesIO(chem.read_bytes()), "chem.csv")}
    r = app_client.post(f"/reports/{rid}/lab-upload", data=data,
                        content_type="multipart/form-data", follow_redirects=True)
    assert b"Uploaded 16 lab result" in r.data
    n = conn.execute(
        "SELECT count(*) c FROM sample s JOIN result rs ON rs.sample_id=s.id "
        "WHERE s.bloom_report_id=%s AND rs.data_type='Laboratory'", (rid,)).fetchone()["c"]
    assert n == 16


def test_post_advisory_shows_on_map(app_client, conn):
    _login(app_client, "staff@wb.ca.gov", "staffpw")
    app_client.post("/reports/new", data={"waterbody": "Advisory Pond", "region": R5,
                                          "lat": "38.5", "lon": "-121.4"}, follow_redirects=True)
    rid = conn.execute(
        "SELECT bloom_report_id FROM event e JOIN location l ON l.id=e.location_id "
        "JOIN waterbody w ON w.id=l.waterbody_id WHERE w.water_body_name='Advisory Pond'"
    ).fetchone()["bloom_report_id"]

    r = app_client.post(f"/reports/{rid}/responses",
                        data={"response_category": "Advisory", "advisory_recommended": "Warning",
                              "display_advisory_on_map": "1"}, follow_redirects=True)
    assert b"Response recorded" in r.data

    import json
    fc = json.loads(app_client.get("/api/reports.geojson").data)
    feat = next(f for f in fc["features"] if f["properties"]["bloom_report_id"] == rid)
    assert feat["properties"]["advisory"] == "Warning"  # the posted advisory appears on the map


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


def test_batch_update_outcomes(app_client, conn):
    _login(app_client, "staff@wb.ca.gov", "staffpw")
    # Two reports in Region 5.
    ids = []
    for name in ("Batch A", "Batch B"):
        app_client.post("/reports/new", data={"waterbody": name, "region": R5,
                                              "confirm_new_wb": "1"}, follow_redirects=True)
        ids.append(conn.execute(
            "SELECT bloom_report_id FROM event e JOIN location l ON l.id=e.location_id "
            "JOIN waterbody w ON w.id=l.waterbody_id WHERE w.water_body_name=%s", (name,)
        ).fetchone()["bloom_report_id"])

    assert app_client.get("/batch").status_code == 200
    r = app_client.post("/batch", data={"report_ids": [str(i) for i in ids],
                                        "determination_code": "no_bloom"}, follow_redirects=True)
    assert b"Updated outcome for 2 report" in r.data
    codes = {row["determination_code"] for row in conn.execute(
        "SELECT determination_code FROM event WHERE bloom_report_id = ANY(%s)", (ids,)).fetchall()}
    assert codes == {"no_bloom"}


def test_batch_filters_by_outcome(app_client, conn):
    _login(app_client, "staff@wb.ca.gov", "staffpw")
    from fhab.reports import enter_report
    staff = conn.execute("SELECT id FROM app_user WHERE email='staff@wb.ca.gov'").fetchone()["id"]
    enter_report(conn, staff, water_body_name="Confirmed Lake", region=R5, determination="confirmed_hab")
    enter_report(conn, staff, water_body_name="Algae Pond", region=R5, determination="non_hab_algae")

    r = app_client.get("/batch?outcome=confirmed_hab")
    assert b"Confirmed Lake" in r.data
    assert b"Algae Pond" not in r.data


def test_report_detail_shows_locations_and_geoconnex(app_client, conn):
    _login(app_client, "staff@wb.ca.gov", "staffpw")
    app_client.post("/reports/new", data={"waterbody": "Loc Pond", "region": R5,
                                          "lat": "38.6", "lon": "-121.5"}, follow_redirects=True)
    rid = conn.execute(
        "SELECT bloom_report_id FROM event e JOIN location l ON l.id=e.location_id "
        "JOIN waterbody w ON w.id=l.waterbody_id WHERE w.water_body_name='Loc Pond'"
    ).fetchone()["bloom_report_id"]
    r = app_client.get(f"/reports/{rid}")
    assert b"Locations &amp; GeoConnex" in r.data
    assert b"Reporting location" in r.data
    # The proposed event PID shows even though it is not minted.
    assert f"https://geoconnex.us/ca-fhab/events/{rid}".encode() in r.data
    assert b"proposed" in r.data


def test_case_create_assign_and_view(app_client, conn):
    _login(app_client, "staff@wb.ca.gov", "staffpw")
    # Create a case.
    r = app_client.post("/cases/new", data={"waterbody": "Web Case Lake", "region": R5,
                                            "year": "2026", "case_lead": "Staffer"},
                        follow_redirects=True)
    assert b"created" in r.data
    cid = conn.execute("SELECT case_id FROM hab_case WHERE case_water_body_name='Web Case Lake'").fetchone()["case_id"]

    # A report to assign.
    app_client.post("/reports/new", data={"waterbody": "Web Case Lake", "region": R5}, follow_redirects=True)
    rid = conn.execute(
        "SELECT bloom_report_id FROM event e JOIN location l ON l.id=e.location_id "
        "JOIN waterbody w ON w.id=l.waterbody_id WHERE w.water_body_name='Web Case Lake'"
    ).fetchone()["bloom_report_id"]

    r = app_client.post(f"/cases/{cid}/assign", data={"brid": str(rid)}, follow_redirects=True)
    assert b"assigned to case" in r.data
    # The report now shows in the case detail.
    r = app_client.get(f"/cases/{cid}")
    assert str(rid).encode() in r.data
    assert b"Reports in this case" in r.data


def test_cases_list_visible(app_client, conn):
    _login(app_client, "staff@wb.ca.gov", "staffpw")
    assert app_client.get("/cases").status_code == 200


def test_non_staff_cannot_create_case(app_client, conn):
    from fhab.auth import set_password
    pub = create_user(conn, "v3@public.org"); grant_role(conn, pub, "public")
    set_password(conn, pub, "pw")
    _login(app_client, "v3@public.org", "pw")
    r = app_client.get("/cases/new", follow_redirects=True)
    assert b"Staff access required" in r.data


def test_dashboard_shows_recent_worked_reports(app_client, conn):
    _login(app_client, "staff@wb.ca.gov", "staffpw")
    # Work on two reports.
    for name in ("Recent A", "Recent B"):
        app_client.post("/reports/new", data={"waterbody": name, "region": R5,
                                              "confirm_new_wb": "1"}, follow_redirects=True)
    r = app_client.get("/")
    assert b"Reports you&#39;ve worked on" in r.data or b"Reports you've worked on" in r.data
    assert b"Recent A" in r.data and b"Recent B" in r.data


def test_update_report_quick_action(app_client, conn):
    _login(app_client, "staff@wb.ca.gov", "staffpw")
    app_client.post("/reports/new", data={"waterbody": "Quick Pond", "region": R5}, follow_redirects=True)
    rid = conn.execute(
        "SELECT bloom_report_id FROM event e JOIN location l ON l.id=e.location_id "
        "JOIN waterbody w ON w.id=l.waterbody_id WHERE w.water_body_name='Quick Pond'"
    ).fetchone()["bloom_report_id"]
    r = app_client.get(f"/reports/go?brid={rid}")
    assert r.status_code == 302 and f"/reports/{rid}" in r.headers["Location"]
    r = app_client.get("/reports/go?brid=", follow_redirects=True)
    assert b"Enter a report ID" in r.data


def test_new_report_form_has_official_vocabularies(app_client, conn):
    _login(app_client, "staff@wb.ca.gov", "staffpw")
    r = app_client.get("/reports/new")
    assert b"between a football field and a tennis court" in r.data  # size vocab
    assert b"Benthic mats" in r.data and b"Suspected illness or death" in r.data
    assert b"Reporter contact" in r.data


def test_new_report_has_county_dropdown(app_client, conn):
    _login(app_client, "staff@wb.ca.gov", "staffpw")
    r = app_client.get("/reports/new")
    assert b'<select name="county">' in r.data and b"Sacramento" in r.data and b"Arizona State" in r.data


def test_waterbody_suggest_api(app_client, conn):
    conn.execute("INSERT INTO waterbody (water_body_name, county) VALUES ('Clear Lake', 'Lake')")
    conn.commit()
    _login(app_client, "staff@wb.ca.gov", "staffpw")
    r = app_client.get("/api/waterbodies?q=clear")
    assert r.status_code == 200 and any(w["name"] == "Clear Lake" for w in r.get_json())


def test_new_report_near_duplicate_guard(app_client, conn):
    conn.execute("INSERT INTO waterbody (water_body_name, county) VALUES ('Clear Lake', 'Lake')")
    conn.commit()
    _login(app_client, "staff@wb.ca.gov", "staffpw")
    # A near-duplicate name is held back with a guard rather than creating a new waterbody.
    r = app_client.post("/reports/new", data={"waterbody": "Clearlake", "region": R5},
                        follow_redirects=True)
    assert b"Possible duplicate waterbody" in r.data
    assert conn.execute("SELECT count(*) c FROM waterbody WHERE water_body_name='Clearlake'"
                        ).fetchone()["c"] == 0
    # A case-variant of the existing name (same county) reuses the canonical row — no duplicate.
    app_client.post("/reports/new", data={"waterbody": "clear lake", "region": R5,
                                          "county": "Lake"}, follow_redirects=True)
    assert conn.execute("SELECT count(*) c FROM waterbody WHERE lower(water_body_name)='clear lake'"
                        ).fetchone()["c"] == 1


def test_new_report_saves_official_fields_and_illness(app_client, conn):
    _login(app_client, "staff@wb.ca.gov", "staffpw")
    app_client.post("/reports/new", data={
        "waterbody": "Form Lake", "region": R5, "landmark": "Day-use beach",
        "report_type": "Public Reporting", "bloom_size": "smaller than a sedan",
        "signs_posted": "Caution", "bloom_textures": ["Surface scum", "Streaking"],
        "has_pictures": "Yes", "reporter_name": "Pat Reporter",
        "reporter_email": "pat@example.com", "no_illness_observed": "",
        "illness_Dog": "1", "death_Dog": "1", "illness_description": "dog vomiting",
    }, follow_redirects=True)
    rid = conn.execute(
        "SELECT bloom_report_id FROM event e JOIN location l ON l.id=e.location_id "
        "JOIN waterbody w ON w.id=l.waterbody_id WHERE w.water_body_name='Form Lake'"
    ).fetchone()["bloom_report_id"]
    ev = conn.execute(
        "SELECT bloom_textures, signs_posted, reporter_name FROM event WHERE bloom_report_id=%s",
        (rid,)).fetchone()
    assert ev["signs_posted"] == "Caution" and ev["reporter_name"] == "Pat Reporter"
    assert set(ev["bloom_textures"]) == {"Surface scum", "Streaking"}
    ill = conn.execute(
        "SELECT subject, illness, death FROM report_illness WHERE bloom_report_id=%s", (rid,)).fetchone()
    assert ill["subject"] == "Dog" and ill["illness"] and ill["death"]
    # The report page shows the reporter (staff can see PII) and the illness card.
    r = app_client.get(f"/reports/{rid}")
    assert b"Pat Reporter" in r.data and b"Suspected illness or death" in r.data


def test_photo_upload_and_serve_roundtrip(app_client, conn):
    import io
    _login(app_client, "staff@wb.ca.gov", "staffpw")
    app_client.post("/reports/new", data={"waterbody": "Photo Lake", "region": R5},
                    follow_redirects=True)
    rid = conn.execute(
        "SELECT bloom_report_id FROM event e JOIN location l ON l.id=e.location_id "
        "JOIN waterbody w ON w.id=l.waterbody_id WHERE w.water_body_name='Photo Lake'"
    ).fetchone()["bloom_report_id"]
    png = bytes.fromhex("89504e470d0a1a0a")  # PNG magic header is enough for a roundtrip
    r = app_client.post(f"/reports/{rid}/photos",
                        data={"photo": (io.BytesIO(png), "bloom.png")}, follow_redirects=True)
    assert b"Photo uploaded" in r.data
    pid = conn.execute("SELECT id FROM report_photo WHERE bloom_report_id=%s", (rid,)).fetchone()["id"]
    r = app_client.get(f"/reports/{rid}/photos/{pid}")
    assert r.status_code == 200 and r.data == png


def test_batch_ceden_upload(app_client, conn):
    import io
    from pathlib import Path
    _login(app_client, "staff@wb.ca.gov", "staffpw")
    chem = Path(__file__).parent / "fixtures" / "ceden" / "CEDEN_WaterChemistry.csv"
    field = Path(__file__).parent / "fixtures" / "ceden" / "CEDEN_FieldResults.csv"
    data = {"chem_file": (io.BytesIO(chem.read_bytes()), "chem.csv"),
            "field_file": (io.BytesIO(field.read_bytes()), "field.csv")}
    r = app_client.post("/batch/ceden", data=data, content_type="multipart/form-data",
                        follow_redirects=True)
    assert b"Ingested 16 result" in r.data
    assert conn.execute("SELECT count(*) c FROM station").fetchone()["c"] == 4
    assert conn.execute("SELECT count(*) c FROM result").fetchone()["c"] == 16


def test_batch_ceden_pull_from_url(app_client, conn):
    from pathlib import Path
    _login(app_client, "staff@wb.ca.gov", "staffpw")
    chem = Path(__file__).parent / "fixtures" / "ceden" / "CEDEN_WaterChemistry.csv"
    r = app_client.post("/batch/ceden", data={"url": chem.resolve().as_uri()},
                        follow_redirects=True)
    assert b"Ingested 16 result" in r.data
    assert conn.execute("SELECT count(*) c FROM result").fetchone()["c"] == 16


def test_batch_ceden_requires_staff(app_client, conn):
    from fhab.auth import set_password
    pub = create_user(conn, "v2@public.org"); grant_role(conn, pub, "public")
    set_password(conn, pub, "pw")
    _login(app_client, "v2@public.org", "pw")
    r = app_client.get("/batch/ceden", follow_redirects=True)
    assert b"Staff access required" in r.data


def test_non_staff_cannot_batch(app_client, conn):
    pub = create_user(conn, "viewer@public.org"); grant_role(conn, pub, "public")
    from fhab.auth import set_password
    set_password(conn, pub, "pw")
    _login(app_client, "viewer@public.org", "pw")
    r = app_client.get("/batch", follow_redirects=True)
    assert b"Staff access required" in r.data


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
