"""Tests for the cross-report lab results browser: filters, sort, count, and the web screen."""

import pytest

from fhab.auth import create_user, grant_role, set_password
from fhab.labquery import count_results, filter_options, query_results
from fhab.reports import add_result, enter_report

R5 = "Region 5 - Central Valley"
R1 = "Region 1 - North Coast"


def _seed(conn):
    staff = create_user(conn, "lab@wb.ca.gov")
    grant_role(conn, staff, "wb_staff", region=R5)
    # Two reports with results: a microcystin hit and a non-detect anatoxin.
    r5 = enter_report(conn, staff, water_body_name="Clear Lake", region=R5)
    add_result(conn, staff, r5, data_type="Laboratory", measurement_value=12.5,
               measurement_unit="ug/L", method="ELISA", sample_date="2026-06-10")
    r1 = enter_report(conn, staff, water_body_name="Pyramid Lake", region=R1)
    add_result(conn, staff, r1, data_type="Laboratory", measurement_value=None,
               res_qual_code="ND", method="ELISA", sample_date="2026-05-01")
    return staff, r5, r1


def test_query_returns_all_results(conn):
    _seed(conn)
    rows = query_results(conn, {})
    assert len(rows) == 2
    assert {r["water_body_name"] for r in rows} == {"Clear Lake", "Pyramid Lake"}


def test_filter_by_region_and_text(conn):
    _seed(conn)
    assert len(query_results(conn, {"region": R5})) == 1
    rows = query_results(conn, {"q": "clear"})
    assert len(rows) == 1 and rows[0]["water_body_name"] == "Clear Lake"


def test_non_detect_filter(conn):
    _seed(conn)
    assert len(query_results(conn, {"nd": "only"})) == 1
    assert len(query_results(conn, {"nd": "exclude"})) == 1


def test_date_filter_and_count(conn):
    _seed(conn)
    f = {"date_from": "2026-06-01"}
    assert count_results(conn, f) == 1
    assert len(query_results(conn, f)) == 1


def test_sort_by_value(conn):
    _seed(conn)
    rows = query_results(conn, {"nd": "exclude"}, sort="value", desc=True)
    assert rows[0]["measurement_value"] is not None


def test_filter_options(conn):
    _seed(conn)
    opts = filter_options(conn)
    assert R5 in opts["regions"] and "Laboratory" in opts["data_types"]


# --- web screen ---

@pytest.fixture()
def client(conn):
    from tests.conftest import TEST_DSN
    from fhab.web import create_app
    staff = create_user(conn, "staff@wb.ca.gov", "Staffer")
    set_password(conn, staff, "pw"); grant_role(conn, staff, "wb_staff", region=R5)
    app = create_app(dsn=TEST_DSN); app.config["TESTING"] = True
    return app.test_client()


def test_lab_screen_and_csv(client, conn):
    _seed(conn)
    client.post("/login", data={"email": "staff@wb.ca.gov", "password": "pw"}, follow_redirects=True)
    r = client.get("/lab")
    assert r.status_code == 200 and b"Clear Lake" in r.data and b"Pyramid Lake" in r.data
    # filtered CSV export
    r = client.get("/lab.csv?region=" + R5.replace(" ", "%20"))
    assert r.status_code == 200 and r.mimetype == "text/csv"
    assert b"Water_Body_Name" in r.data and b"Clear Lake" in r.data and b"Pyramid Lake" not in r.data


def test_lab_screen_requires_staff(client, conn):
    pub = create_user(conn, "v@public.org"); grant_role(conn, pub, "public")
    set_password(conn, pub, "pw")
    client.post("/login", data={"email": "v@public.org", "password": "pw"}, follow_redirects=True)
    assert b"Staff access required" in client.get("/lab", follow_redirects=True).data
