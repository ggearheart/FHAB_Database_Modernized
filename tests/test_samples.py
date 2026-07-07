"""Sample work area: browse, manual create, and edit."""

import pytest

from fhab.auth import create_user, grant_role, set_password
from fhab.samples import count_samples, create_sample, get_sample, list_samples, update_sample

R5 = "Region 5 - Central Valley"


def _staff(conn):
    u = create_user(conn, "wa@wb.ca.gov"); grant_role(conn, u, "wb_staff", region=R5)
    return u


def test_create_edit_and_list(conn):
    staff = _staff(conn)
    sid = create_sample(conn, staff, {
        "station_code": "533KAR020", "station_name": "Kaweah at bridge", "sample_date": "6/2/2025",
        "sample_type": "Water Grab", "bg_id": "WB1", "lab_sample_id": "L001",
        "lat": "36.4", "lon": "-119.1"})
    d = get_sample(conn, sid)
    assert d["sample"]["station_code"] == "533KAR020"
    assert abs(d["sample"]["lat"] - 36.4) < 1e-6           # geocoded on create
    assert d["sample"]["status"] == "unlinked"

    update_sample(conn, staff, sid, {"sample_type": "AlgalMat", "collected_by": "Field crew",
                                     "station_name": "Kaweah — new name", "lat": "36.5", "lon": "-119.2"})
    d2 = get_sample(conn, sid)
    assert d2["sample"]["sample_type"] == "AlgalMat"
    assert d2["sample"]["collected_by"] == "Field crew"
    assert d2["sample"]["station_name"] == "Kaweah — new name"
    assert abs(d2["sample"]["lat"] - 36.5) < 1e-6          # coordinates updated

    # browse / filter
    assert count_samples(conn, {}) >= 1
    assert sid in {r["id"] for r in list_samples(conn, {"q": "533KAR"})}
    assert sid in {r["id"] for r in list_samples(conn, {"geocoded": "yes"})}
    assert sid not in {r["id"] for r in list_samples(conn, {"geocoded": "no"})}


def test_create_without_coords_is_ungeocoded(conn):
    staff = _staff(conn)
    sid = create_sample(conn, staff, {"station_code": "NOGEOX", "sample_date": "2025-06-01"})
    assert get_sample(conn, sid)["sample"]["lat"] is None
    assert sid in {r["id"] for r in list_samples(conn, {"geocoded": "no"})}


# --- web ---

@pytest.fixture()
def client(conn):
    from fhab.web import create_app
    from tests.conftest import TEST_DSN
    staff = create_user(conn, "staff@wb.ca.gov"); set_password(conn, staff, "pw")
    grant_role(conn, staff, "wb_staff", region=R5)
    app = create_app(dsn=TEST_DSN); app.config["TESTING"] = True
    return app.test_client()


def test_sample_area_web(client, conn):
    client.post("/login", data={"email": "staff@wb.ca.gov", "password": "pw"}, follow_redirects=True)
    # create via the form
    r = client.post("/lab/samples/new", data={"station_code": "WEBSAMP1", "sample_date": "2025-06-02",
                    "sample_type": "Water Grab", "lat": "38.5", "lon": "-121.4"}, follow_redirects=True)
    assert r.status_code == 200 and b"Sample created" in r.data
    sid = conn.execute("SELECT id FROM sample WHERE station_id = "
                       "(SELECT id FROM station WHERE station_code='WEBSAMP1')").fetchone()["id"]
    # it shows in the list
    assert b"WEBSAMP1" in client.get("/lab/samples?q=WEBSAMP").data
    # edit it
    client.post(f"/lab/samples/{sid}", data={"sample_type": "AlgalMat", "site": "north end"},
                follow_redirects=True)
    assert conn.execute("SELECT sample_type FROM sample WHERE id=%s", (sid,)).fetchone()["sample_type"] == "AlgalMat"


def test_sample_area_requires_staff(client, conn):
    pub = create_user(conn, "p@public.org"); grant_role(conn, pub, "public"); set_password(conn, pub, "pw")
    client.post("/login", data={"email": "p@public.org", "password": "pw"}, follow_redirects=True)
    assert b"Staff access required" in client.get("/lab/samples", follow_redirects=True).data
