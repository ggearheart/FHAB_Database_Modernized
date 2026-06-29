"""Tests for the maintenance purge: shared helper + the admin Reset screen (type-to-confirm)."""

import pytest

from fhab.auth import create_user, grant_role, set_password
from fhab.maintenance import lab_data_counts, purge_lab_data
from fhab.reports import add_result, enter_report

R5 = "Region 5 - Central Valley"


def _seed_lab(conn):
    staff = create_user(conn, "lab@wb.ca.gov"); grant_role(conn, staff, "wb_staff", region=R5)
    brid = enter_report(conn, staff, water_body_name="Purge Lake", region=R5)
    add_result(conn, staff, brid, data_type="Laboratory", measurement_value=5)
    add_result(conn, staff, brid, data_type="Laboratory", measurement_value=9)
    return brid


def test_purge_deletes_lab_keeps_reports(conn):
    brid = _seed_lab(conn)
    before = lab_data_counts(conn)
    assert before["sample"] >= 1 and before["result"] >= 2
    deleted = purge_lab_data(conn)
    assert deleted["result"] >= 2 and deleted["sample"] >= 1
    after = lab_data_counts(conn)
    assert after["sample"] == 0 and after["result"] == 0
    # reports/events + analyte vocabulary preserved
    assert after["event"] == before["event"] and before["event"] >= 1
    assert conn.execute("SELECT 1 FROM event WHERE bloom_report_id=%s", (brid,)).fetchone()
    assert after["analyte"] == before["analyte"]


# --- admin screen ---

@pytest.fixture()
def client(conn):
    from tests.conftest import TEST_DSN
    from fhab.web import create_app
    admin = create_user(conn, "admin@fhab.local", "Admin")
    set_password(conn, admin, "pw"); grant_role(conn, admin, "program_admin")
    app = create_app(dsn=TEST_DSN); app.config["TESTING"] = True
    return app.test_client()


def test_reset_screen_shows_counts(client, conn):
    _seed_lab(conn)
    client.post("/login", data={"email": "admin@fhab.local", "password": "pw"}, follow_redirects=True)
    r = client.get("/admin/reset")
    assert r.status_code == 200 and b"Purge all lab data" in r.data and b"sample" in r.data


def test_reset_requires_typed_confirmation(client, conn):
    _seed_lab(conn)
    client.post("/login", data={"email": "admin@fhab.local", "password": "pw"}, follow_redirects=True)
    # wrong confirmation -> nothing deleted
    r = client.post("/admin/reset/purge-lab", data={"confirm": "nope"}, follow_redirects=True)
    assert b"Type RESET to confirm" in r.data
    assert conn.execute("SELECT count(*) c FROM sample").fetchone()["c"] >= 1
    # correct confirmation -> purged
    r = client.post("/admin/reset/purge-lab", data={"confirm": "reset"}, follow_redirects=True)
    assert b"Lab data purged" in r.data
    assert conn.execute("SELECT count(*) c FROM sample").fetchone()["c"] == 0


def test_reset_requires_admin(client, conn):
    staff = create_user(conn, "s@wb.ca.gov"); grant_role(conn, staff, "wb_staff", region=R5)
    set_password(conn, staff, "pw")
    client.post("/login", data={"email": "s@wb.ca.gov", "password": "pw"}, follow_redirects=True)
    assert client.get("/admin/reset", follow_redirects=True).status_code in (200, 403)
    assert b"Purge all lab data" not in client.get("/admin/reset", follow_redirects=True).data
    # and the action is blocked
    _seed_lab(conn)
    client.post("/admin/reset/purge-lab", data={"confirm": "reset"}, follow_redirects=True)
    assert conn.execute("SELECT count(*) c FROM sample").fetchone()["c"] >= 1
