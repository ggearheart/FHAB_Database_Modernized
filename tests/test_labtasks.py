"""Tests for the lab-data reconciliation workboard: assign, link orphans, QA, re-reconcile."""

import pytest

from fhab.auth import create_user, grant_role, set_password
from fhab.labtasks import (assign_samples, count_workboard, create_report_from_sample, link_sample,
                           qa_review, status_tallies, team_members, unlink_sample, workboard)
from fhab.reports import add_result, enter_report

R5 = "Region 5 - Central Valley"


def _staff(conn, email="cm@wb.ca.gov"):
    u = create_user(conn, email); grant_role(conn, u, "wb_staff", region=R5)
    return u


def _orphan_sample(conn, station="ORP1"):
    st = conn.execute("INSERT INTO station (station_code, geom) "
                      "VALUES (%s, ST_SetSRID(ST_MakePoint(-121.4,38.5),4326)) RETURNING id",
                      (station,)).fetchone()["id"]
    sid = conn.execute("INSERT INTO sample (station_id, sample_date) VALUES (%s, current_date) "
                       "RETURNING id", (st,)).fetchone()["id"]
    conn.execute("INSERT INTO result (result_id_unique, sample_id, data_type) "
                 "VALUES (%s, %s, 'Laboratory')", (f"r-{sid}", sid))
    conn.commit()
    return sid


def _status(conn, sid):
    return next(r["status"] for r in workboard(conn, {}) if r["id"] == sid)


def test_link_orphan_then_qa(conn):
    staff = _staff(conn)
    brid = enter_report(conn, staff, water_body_name="Link Lake", region=R5)
    sid = _orphan_sample(conn)
    assert _status(conn, sid) == "unlinked"

    link_sample(conn, staff, sid, bloom_report_id=brid)
    row = conn.execute("SELECT bloom_report_id, qa_status FROM sample WHERE id=%s", (sid,)).fetchone()
    assert row["bloom_report_id"] == brid and row["qa_status"] is None
    assert _status(conn, sid) == "linked"

    qa_review(conn, staff, sid, approve=True)
    assert _status(conn, sid) == "approved"


def test_relink_clears_qa_for_re_review(conn):
    staff = _staff(conn)
    a = enter_report(conn, staff, water_body_name="A", region=R5)
    b = enter_report(conn, staff, water_body_name="B", region=R5)
    sid = _orphan_sample(conn)
    link_sample(conn, staff, sid, bloom_report_id=a)
    qa_review(conn, staff, sid, approve=True)
    assert _status(conn, sid) == "approved"
    # Revisit: re-link to a different report -> QA cleared, back to 'linked'.
    link_sample(conn, staff, sid, bloom_report_id=b)
    row = conn.execute("SELECT bloom_report_id, qa_status FROM sample WHERE id=%s", (sid,)).fetchone()
    assert row["bloom_report_id"] == b and row["qa_status"] is None
    assert _status(conn, sid) == "linked"


def test_flag_then_unlink(conn):
    staff = _staff(conn)
    brid = enter_report(conn, staff, water_body_name="Flag Lake", region=R5)
    sid = _orphan_sample(conn)
    link_sample(conn, staff, sid, bloom_report_id=brid)
    qa_review(conn, staff, sid, approve=False, note="wrong waterbody")
    assert _status(conn, sid) == "flagged"
    assert conn.execute("SELECT qa_note FROM sample WHERE id=%s", (sid,)).fetchone()["qa_note"] == "wrong waterbody"
    unlink_sample(conn, staff, sid)
    assert _status(conn, sid) == "unlinked"


def test_assign_and_filter_by_assignee(conn):
    boss = _staff(conn, "boss@wb.ca.gov")
    member = create_user(conn, "member@wb.ca.gov"); grant_role(conn, member, "field_staff")
    s1, s2 = _orphan_sample(conn, "S1"), _orphan_sample(conn, "S2")
    assert assign_samples(conn, boss, [s1, s2], member) == 2
    mine = workboard(conn, {"assignee": str(member)})
    assert {r["id"] for r in mine} == {s1, s2}
    assert all(r["assignee"] == "member@wb.ca.gov" for r in mine)
    assert member in {m["id"] for m in team_members(conn)}


def test_create_report_from_sample_links_it(conn):
    staff = _staff(conn)
    sid = _orphan_sample(conn, "MKRPT")
    brid = create_report_from_sample(conn, staff, sid, region=R5)
    row = conn.execute("SELECT bloom_report_id FROM sample WHERE id=%s", (sid,)).fetchone()
    assert row["bloom_report_id"] == brid
    rt = conn.execute("SELECT report_type FROM event WHERE bloom_report_id=%s", (brid,)).fetchone()
    assert rt["report_type"] == "Lab data"


def test_tallies_and_count(conn):
    staff = _staff(conn)
    brid = enter_report(conn, staff, water_body_name="T", region=R5)
    s1, s2 = _orphan_sample(conn, "T1"), _orphan_sample(conn, "T2")
    link_sample(conn, staff, s1, bloom_report_id=brid)
    t = status_tallies(conn)
    assert t.get("linked", 0) >= 1 and t.get("unlinked", 0) >= 1
    assert count_workboard(conn, {"status": "unlinked"}) >= 1


# --- web ---

@pytest.fixture()
def client(conn):
    from tests.conftest import TEST_DSN
    from fhab.web import create_app
    staff = create_user(conn, "staff@wb.ca.gov", "Staffer")
    set_password(conn, staff, "pw"); grant_role(conn, staff, "wb_staff", region=R5)
    app = create_app(dsn=TEST_DSN); app.config["TESTING"] = True
    return app.test_client()


def test_workboard_screen_and_link_via_web(client, conn):
    staff = create_user(conn, "s2@wb.ca.gov"); grant_role(conn, staff, "wb_staff", region=R5)
    brid = enter_report(conn, staff, water_body_name="Web Link Lake", region=R5)
    sid = _orphan_sample(conn, "WEB1")
    client.post("/login", data={"email": "staff@wb.ca.gov", "password": "pw"}, follow_redirects=True)
    r = client.get("/lab/workboard?status=unlinked")
    assert r.status_code == 200 and b"WEB1" in r.data
    client.post(f"/lab/sample/{sid}/link", data={"bloom_report_id": str(brid)}, follow_redirects=True)
    assert conn.execute("SELECT bloom_report_id FROM sample WHERE id=%s", (sid,)).fetchone()["bloom_report_id"] == brid
    # QA via web
    client.post(f"/lab/sample/{sid}/qa", data={"action": "approve"}, follow_redirects=True)
    assert conn.execute("SELECT qa_status FROM sample WHERE id=%s", (sid,)).fetchone()["qa_status"] == "approved"


def test_sample_geo_context(conn):
    from fhab.labtasks import sample_geo
    staff = _staff(conn)
    # A report ~near the station so it shows as a candidate.
    near = enter_report(conn, staff, water_body_name="Near Lake", region=R5,
                        lat=38.5, lon=-121.401)   # ~ a few hundred m from the ORP1 station
    sid = _orphan_sample(conn, "GEO1")            # station at (-121.4, 38.5)
    g = sample_geo(conn, sid)
    assert g["station"] and g["station"]["code"] == "GEO1"
    assert g["sample_date"]                                   # actual sample date for the tooltip
    assert g["linked"] is None
    cand = next(c for c in g["candidates"] if c["brid"] == near)
    assert cand["obs"]                                        # candidate's actual observation date

    # Once linked, it appears as 'linked' (with its date) and drops out of candidates.
    link_sample(conn, staff, sid, bloom_report_id=near)
    g2 = sample_geo(conn, sid)
    assert g2["linked"] and g2["linked"]["brid"] == near and "obs" in g2["linked"]
    assert all(c["brid"] != near for c in g2["candidates"])


def test_batch_reconcile_links_confident_skips_others(conn):
    from fhab.labtasks import batch_reconcile_samples
    staff = _staff(conn)
    # A report right next to the station and same date -> confident match.
    near = enter_report(conn, staff, water_body_name="Match Lake", region=R5,
                        lat=38.5, lon=-121.401, observation_date="2026-06-15")
    st = conn.execute("INSERT INTO station (station_code, geom) "
                      "VALUES ('M1', ST_SetSRID(ST_MakePoint(-121.4,38.5),4326)) RETURNING id"
                      ).fetchone()["id"]
    s_match = conn.execute("INSERT INTO sample (station_id, sample_date) VALUES (%s,'2026-06-15') "
                           "RETURNING id", (st,)).fetchone()["id"]
    conn.execute("INSERT INTO result (result_id_unique, sample_id, data_type) VALUES ('m1',%s,'Laboratory')",
                 (s_match,))
    # A sample with no nearby report (far station) -> should be skipped.
    far = conn.execute("INSERT INTO station (station_code, geom) "
                       "VALUES ('F1', ST_SetSRID(ST_MakePoint(-118.0,34.0),4326)) RETURNING id"
                       ).fetchone()["id"]
    s_far = conn.execute("INSERT INTO sample (station_id, sample_date) VALUES (%s,'2026-06-15') "
                         "RETURNING id", (far,)).fetchone()["id"]
    conn.execute("INSERT INTO result (result_id_unique, sample_id, data_type) VALUES ('f1',%s,'Laboratory')",
                 (s_far,))
    conn.commit()

    res = batch_reconcile_samples(conn, [s_match, s_far], days=14)
    assert res["linked"] == 1 and res["skipped"] == 1
    assert conn.execute("SELECT bloom_report_id FROM sample WHERE id=%s", (s_match,)).fetchone()["bloom_report_id"] == near
    assert conn.execute("SELECT bloom_report_id FROM sample WHERE id=%s", (s_far,)).fetchone()["bloom_report_id"] is None


def test_batch_reconcile_via_web_filter(client, conn):
    from fhab.reports import enter_report
    staff = create_user(conn, "rec@wb.ca.gov"); grant_role(conn, staff, "wb_staff", region=R5)
    near = enter_report(conn, staff, water_body_name="WebMatch Lake", region=R5,
                        lat=38.5, lon=-121.401, observation_date="2026-06-15")
    st = conn.execute("INSERT INTO station (station_code, geom) "
                      "VALUES ('WB-M', ST_SetSRID(ST_MakePoint(-121.4,38.5),4326)) RETURNING id"
                      ).fetchone()["id"]
    sid = conn.execute("INSERT INTO sample (station_id, sample_date) VALUES (%s,'2026-06-15') "
                       "RETURNING id", (st,)).fetchone()["id"]
    conn.execute("INSERT INTO result (result_id_unique, sample_id, data_type) VALUES ('wbm',%s,'Laboratory')",
                 (sid,)); conn.commit()
    client.post("/login", data={"email": "staff@wb.ca.gov", "password": "pw"}, follow_redirects=True)
    # no sample_ids -> reconcile the filtered (unlinked) batch
    r = client.post("/lab/workboard/reconcile", data={"days": "14"}, follow_redirects=True)
    assert b"Batch reconcile" in r.data
    assert conn.execute("SELECT bloom_report_id FROM sample WHERE id=%s", (sid,)).fetchone()["bloom_report_id"] == near


def test_sample_geo_endpoint(client, conn):
    sid = _orphan_sample(conn, "GEOWEB")
    client.post("/login", data={"email": "staff@wb.ca.gov", "password": "pw"}, follow_redirects=True)
    j = client.get(f"/lab/sample/{sid}/geo.json").get_json()
    assert j["station"]["code"] == "GEOWEB" and "candidates" in j


def test_set_location_geocodes_and_probe_finds_reports(conn):
    from fhab.labtasks import sample_geo, set_sample_location
    staff = _staff(conn)
    near = enter_report(conn, staff, water_body_name="Probe Lake", region=R5,
                        lat=38.5, lon=-121.401, observation_date="2026-06-15")
    st = conn.execute("INSERT INTO station (station_code) VALUES ('NOGEO1') RETURNING id").fetchone()["id"]
    sid = conn.execute("INSERT INTO sample (station_id, sample_date) VALUES (%s,'2026-06-15') "
                       "RETURNING id", (st,)).fetchone()["id"]
    conn.execute("INSERT INTO result (result_id_unique, sample_id, data_type) VALUES ('ng1',%s,'Laboratory')", (sid,))
    conn.commit()

    g0 = sample_geo(conn, sid)
    assert g0["station"] is None and g0["candidates"] == []          # nothing to anchor on
    g1 = sample_geo(conn, sid, at=(38.5, -121.4))                    # probe around CoC coords
    assert g1["probe"] == {"lat": 38.5, "lon": -121.4}
    assert any(c["brid"] == near for c in g1["candidates"])

    set_sample_location(conn, sid, 38.5, -121.4)                     # persist -> geocoded
    g2 = sample_geo(conn, sid)
    assert g2["station"] and abs(g2["station"]["lat"] - 38.5) < 1e-6


def test_set_location_validates_range(conn):
    from fhab.labtasks import set_sample_location
    sid = _orphan_sample(conn, "RNG1")
    with pytest.raises(ValueError):
        set_sample_location(conn, sid, 999, -121.4)


def test_geocode_and_ocr_routes_web(client, conn):
    staff = create_user(conn, "geo@wb.ca.gov"); grant_role(conn, staff, "wb_staff", region=R5)
    near = enter_report(conn, staff, water_body_name="WebProbe Lake", region=R5,
                        lat=38.5, lon=-121.401, observation_date="2026-06-15")
    st = conn.execute("INSERT INTO station (station_code) VALUES ('WEBNOGEO') RETURNING id").fetchone()["id"]
    sid = conn.execute("INSERT INTO sample (station_id, sample_date) VALUES (%s,'2026-06-15') RETURNING id",
                       (st,)).fetchone()["id"]
    conn.execute("INSERT INTO result (result_id_unique, sample_id, data_type) VALUES ('wg1',%s,'Laboratory')", (sid,)); conn.commit()
    client.post("/login", data={"email": "staff@wb.ca.gov", "password": "pw"}, follow_redirects=True)

    j = client.get(f"/lab/sample/{sid}/geo.json?lat=38.5&lon=-121.4").get_json()
    assert j["probe"]["lat"] == 38.5 and any(c["brid"] == near for c in j["candidates"])
    r = client.post(f"/lab/sample/{sid}/geocode", data={"lat": "38.5", "lon": "-121.4"})
    assert r.status_code == 200 and r.get_json()["station"]["lat"]
    assert conn.execute("SELECT geom IS NOT NULL AS g FROM station WHERE id=%s", (st,)).fetchone()["g"]
    assert client.get(f"/lab/sample/{sid}/ocr-coords").status_code == 404   # no CoC -> graceful


def test_workboard_requires_staff(client, conn):
    pub = create_user(conn, "v@public.org"); grant_role(conn, pub, "public")
    set_password(conn, pub, "pw")
    client.post("/login", data={"email": "v@public.org", "password": "pw"}, follow_redirects=True)
    assert b"Staff access required" in client.get("/lab/workboard", follow_redirects=True).data
