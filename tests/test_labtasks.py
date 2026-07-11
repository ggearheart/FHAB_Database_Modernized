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


def test_tag_routine_status_and_untag(conn):
    from fhab.labtasks import clear_routine, status_tallies, tag_routine
    _staff(conn)
    sid = _orphan_sample(conn, "ROUT1")
    assert _status(conn, sid) == "unlinked"
    tag_routine(conn, _staff(conn, "r2@wb.ca.gov"), sid)
    assert _status(conn, sid) == "routine"
    assert status_tallies(conn).get("routine", 0) >= 1
    clear_routine(conn, _staff(conn, "r3@wb.ca.gov"), sid)
    assert _status(conn, sid) == "unlinked"


def test_routine_and_create_report_with_coords_web(client, conn):
    from fhab.reports import enter_report
    staff = create_user(conn, "rt@wb.ca.gov"); grant_role(conn, staff, "wb_staff", region=R5)
    client.post("/login", data={"email": "staff@wb.ca.gov", "password": "pw"}, follow_redirects=True)

    # (b) tag routine, then undo
    s1 = _orphan_sample(conn, "WROUT")
    client.post(f"/lab/sample/{s1}/routine", follow_redirects=True)
    assert conn.execute("SELECT sampling_type FROM sample WHERE id=%s", (s1,)).fetchone()["sampling_type"] == "routine"
    client.post(f"/lab/sample/{s1}/routine", data={"undo": "1"}, follow_redirects=True)
    assert conn.execute("SELECT sampling_type FROM sample WHERE id=%s", (s1,)).fetchone()["sampling_type"] is None

    # (a) create a report from an ungeocoded sample using coordinates from the map
    st = conn.execute("INSERT INTO station (station_code) VALUES ('CRPT') RETURNING id").fetchone()["id"]
    s2 = conn.execute("INSERT INTO sample (station_id, sample_date) VALUES (%s, current_date) RETURNING id",
                      (st,)).fetchone()["id"]
    conn.execute("INSERT INTO result (result_id_unique, sample_id, data_type) VALUES ('crp',%s,'Laboratory')", (s2,)); conn.commit()
    client.post(f"/lab/sample/{s2}/create-report", data={"lat": "38.5", "lon": "-121.4", "region": R5},
                follow_redirects=True)
    row = conn.execute("SELECT bloom_report_id FROM sample WHERE id=%s", (s2,)).fetchone()
    assert row["bloom_report_id"] is not None
    assert conn.execute("SELECT geom IS NOT NULL AS g FROM station WHERE id=%s", (st,)).fetchone()["g"]


def test_bulk_geocode_parses_and_matches(conn):
    from fhab.labtasks import bulk_geocode, parse_coord_rows
    staff = _staff(conn)
    # names with commas/spaces still parse (last two numbers are lat/lon)
    rows = parse_coord_rows("El Dorado E. RP, North Pond, 33.79, -118.09\nbad line")
    assert rows[0]["key"] == "El Dorado E. RP, North Pond" and rows[0]["lat"] == 33.79
    assert rows[1]["status"] == "unparsed"

    st = conn.execute("INSERT INTO station (station_code) VALUES ('BULK1') RETURNING id").fetchone()["id"]
    sid = conn.execute("INSERT INTO sample (station_id, sample_date) VALUES (%s, current_date) RETURNING id",
                       (st,)).fetchone()["id"]
    conn.execute("INSERT INTO result (result_id_unique, sample_id, data_type) VALUES ('b1',%s,'Laboratory')", (sid,)); conn.commit()
    res = bulk_geocode(conn, staff, "BULK1, 38.5, -121.4\nNOPE, 39.0, -122.0\nBULK1, 999, 0")
    assert res["applied"] == 1 and res["samples"] == 1
    assert conn.execute("SELECT geom IS NOT NULL AS g FROM station WHERE id=%s", (st,)).fetchone()["g"]
    assert any("no matching" in r["status"] for r in res["rows"])
    assert any("out of range" in r["status"] for r in res["rows"])


def test_bulk_coordinates_web(client, conn):
    from fhab.auth import create_user, grant_role
    create_user(conn, "bc@wb.ca.gov")
    st = conn.execute("INSERT INTO station (station_code) VALUES ('WEBBULK') RETURNING id").fetchone()["id"]
    sid = conn.execute("INSERT INTO sample (station_id, sample_date) VALUES (%s, current_date) RETURNING id",
                       (st,)).fetchone()["id"]
    conn.execute("INSERT INTO result (result_id_unique, sample_id, data_type) VALUES ('wb2',%s,'Laboratory')", (sid,)); conn.commit()
    client.post("/login", data={"email": "staff@wb.ca.gov", "password": "pw"}, follow_redirects=True)
    r = client.post("/lab/coordinates", data={"rows": "WEBBULK, 38.5, -121.4"}, follow_redirects=True)
    assert r.status_code == 200
    assert conn.execute("SELECT geom IS NOT NULL AS g FROM station WHERE id=%s", (st,)).fetchone()["g"]


def test_link_to_reports_single_and_shared_case(conn):
    from fhab.cases import create_case, assign_report_to_case
    from fhab.labtasks import link_sample_to_reports
    staff = _staff(conn)
    a = enter_report(conn, staff, water_body_name="Multi A", region=R5)
    b = enter_report(conn, staff, water_body_name="Multi B", region=R5)
    sid = _orphan_sample(conn, "MULTI")

    # one report -> links to that report
    r1 = link_sample_to_reports(conn, staff, sid, ["", str(a), "x"])
    assert r1 == {"linked": "report", "id": a}
    assert conn.execute("SELECT bloom_report_id FROM sample WHERE id=%s", (sid,)).fetchone()["bloom_report_id"] == a

    # two reports in one case -> links to the case
    cid = create_case(conn, staff, water_body_name="Multi Case", region=R5)
    assign_report_to_case(conn, staff, a, cid); assign_report_to_case(conn, staff, b, cid)
    r2 = link_sample_to_reports(conn, staff, sid, [str(a), str(b)])
    assert r2["linked"] == "case" and r2["id"] == cid
    row = conn.execute("SELECT bloom_report_id, case_id FROM sample WHERE id=%s", (sid,)).fetchone()
    assert row["case_id"] == cid and row["bloom_report_id"] is None


def test_link_to_reports_no_shared_case_errors(conn):
    from fhab.labtasks import link_sample_to_reports
    staff = _staff(conn)
    a = enter_report(conn, staff, water_body_name="NoCase A", region=R5)
    b = enter_report(conn, staff, water_body_name="NoCase B", region=R5)
    sid = _orphan_sample(conn, "NOCASE")
    res = link_sample_to_reports(conn, staff, sid, [str(a), str(b)])
    assert "error" in res
    assert conn.execute("SELECT bloom_report_id, case_id FROM sample WHERE id=%s", (sid,)).fetchone()["bloom_report_id"] is None


def test_link_selected_via_web(client, conn):
    from fhab.cases import create_case, assign_report_to_case
    staff = create_user(conn, "ls@wb.ca.gov"); grant_role(conn, staff, "wb_staff", region=R5)
    a = enter_report(conn, staff, water_body_name="WebMulti A", region=R5)
    b = enter_report(conn, staff, water_body_name="WebMulti B", region=R5)
    cid = create_case(conn, staff, water_body_name="WebMulti Case", region=R5)
    assign_report_to_case(conn, staff, a, cid); assign_report_to_case(conn, staff, b, cid)
    sid = _orphan_sample(conn, "WEBMULTI")
    client.post("/login", data={"email": "staff@wb.ca.gov", "password": "pw"}, follow_redirects=True)
    r = client.post(f"/lab/sample/{sid}/link-selected",
                    data={"report_ids": [str(a), str(b)]}, follow_redirects=True)
    assert r.status_code == 200
    assert conn.execute("SELECT case_id FROM sample WHERE id=%s", (sid,)).fetchone()["case_id"] == cid


def test_sample_geo_includes_batch_files(conn):
    from fhab.labtasks import sample_geo
    _staff(conn)
    bid = conn.execute("INSERT INTO lab_batch (kind, source) VALUES ('ingested','B') RETURNING id").fetchone()["id"]
    conn.execute("INSERT INTO lab_batch_file (batch_id, category, filename, data) "
                 "VALUES (%s,'coc','COC_x.pdf', %s)", (bid, b"%PDF"))
    sid = _orphan_sample(conn, "FILESAMP")
    conn.execute("UPDATE sample SET lab_batch_id=%s WHERE id=%s", (bid, sid)); conn.commit()
    conn.execute("UPDATE sample SET sample_type='AlgalMat', bg_id='WB9', project_code='RCMP' WHERE id=%s", (sid,)); conn.commit()
    g = sample_geo(conn, sid)
    assert g["files"] and g["files"][0]["category"] == "coc"
    assert g["files"][0]["batch_id"] == bid and g["files"][0]["filename"] == "COC_x.pdf"
    # summary reflects the folder-ingest provenance + identity
    assert g["summary"]["source"].startswith("Email folder ingest")
    assert g["summary"]["bg_id"] == "WB9" and g["summary"]["file_categories"] == ["coc"]

    # a sample with no ingest batch, but CEDEN identity -> labeled as a CEDEN upload, no files
    other = _orphan_sample(conn, "NOFILE")
    conn.execute("UPDATE sample SET bg_id='WB10' WHERE id=%s", (other,)); conn.commit()
    g2 = sample_geo(conn, other)
    assert g2["files"] == [] and g2["summary"]["source"] == "CEDEN chemistry upload"
    assert g2["summary"]["n_files"] == 0


def test_geocoded_filter_and_tally(conn):
    _staff(conn)
    geo = _orphan_sample(conn, "HASGEO")          # _orphan_sample creates a station with geom
    nogeo_st = conn.execute("INSERT INTO station (station_code) VALUES ('NOGEO') RETURNING id").fetchone()["id"]
    nogeo = conn.execute("INSERT INTO sample (station_id, sample_date) VALUES (%s, current_date) "
                         "RETURNING id", (nogeo_st,)).fetchone()["id"]
    conn.execute("INSERT INTO result (result_id_unique, sample_id, data_type) VALUES ('ng',%s,'Laboratory')", (nogeo,))
    conn.commit()

    got = workboard(conn, {"status": "unlinked", "geocoded": "yes"})
    ids = {r["id"] for r in got}
    assert geo in ids and nogeo not in ids
    got_no = workboard(conn, {"status": "unlinked", "geocoded": "no"})
    assert nogeo in {r["id"] for r in got_no} and geo not in {r["id"] for r in got_no}

    t = status_tallies(conn)
    assert t.get("unlinked_geocoded", 0) >= 1 and t.get("unlinked_nogeo", 0) >= 1


def test_geocoded_filter_web(client, conn):
    grant_role(conn, create_user(conn, "gf@wb.ca.gov"), "wb_staff", region=R5)
    geo = _orphan_sample(conn, "WEBGEO")
    client.post("/login", data={"email": "staff@wb.ca.gov", "password": "pw"}, follow_redirects=True)
    r = client.get("/lab/workboard?status=unlinked&geocoded=yes")
    assert r.status_code == 200 and b"WEBGEO" in r.data
    assert b"Geocoded \xc2\xb7 not linked" in r.data     # the chip renders


def test_set_sample_point_splits_shared_station(conn):
    from fhab.labtasks import set_sample_point
    st = conn.execute("INSERT INTO station (station_code) VALUES ('SHARED') RETURNING id").fetchone()["id"]
    s1 = conn.execute("INSERT INTO sample (station_id, bg_id) VALUES (%s,'BGA') RETURNING id", (st,)).fetchone()["id"]
    s2 = conn.execute("INSERT INTO sample (station_id, bg_id) VALUES (%s,'BGB') RETURNING id", (st,)).fetchone()["id"]
    conn.commit()
    set_sample_point(conn, s1, 38.5, -121.4)
    set_sample_point(conn, s2, 39.0, -122.0)
    conn.commit()
    r1 = conn.execute("SELECT st.id, ST_Y(st.geom) AS lat FROM sample s JOIN station st ON st.id=s.station_id WHERE s.id=%s", (s1,)).fetchone()
    r2 = conn.execute("SELECT st.id, ST_Y(st.geom) AS lat FROM sample s JOIN station st ON st.id=s.station_id WHERE s.id=%s", (s2,)).fetchone()
    assert r1["id"] != r2["id"]                      # each got its own station
    assert abs(r1["lat"] - 38.5) < 1e-6 and abs(r2["lat"] - 39.0) < 1e-6


def test_batch_coordinates_screen_web(client, conn):
    bid = conn.execute("INSERT INTO lab_batch (kind, source) VALUES ('ingested','Sacramento Ponds') RETURNING id").fetchone()["id"]
    st = conn.execute("INSERT INTO station (station_code) VALUES ('SACPONDS') RETURNING id").fetchone()["id"]
    s1 = conn.execute("INSERT INTO sample (station_id, lab_batch_id, bg_id) VALUES (%s,%s,'P1') RETURNING id", (st, bid)).fetchone()["id"]
    s2 = conn.execute("INSERT INTO sample (station_id, lab_batch_id, bg_id) VALUES (%s,%s,'P2') RETURNING id", (st, bid)).fetchone()["id"]
    conn.commit()
    client.post("/login", data={"email": "staff@wb.ca.gov", "password": "pw"}, follow_redirects=True)
    page = client.get(f"/lab/batch/{bid}/coordinates")
    assert page.status_code == 200 and b"Enter sample coordinates" in page.data and b"Sacramento Ponds" in page.data
    r = client.post(f"/lab/batch/{bid}/coordinates", data={
        "sample_id": [str(s1), str(s2)],
        f"lat_{s1}": "38.5", f"lon_{s1}": "-121.4",
        f"lat_{s2}": "39.0", f"lon_{s2}": "-122.0"}, follow_redirects=True)
    assert r.status_code == 200
    g1 = conn.execute("SELECT ST_Y(st.geom) AS lat FROM sample s JOIN station st ON st.id=s.station_id WHERE s.id=%s", (s1,)).fetchone()["lat"]
    g2 = conn.execute("SELECT ST_Y(st.geom) AS lat FROM sample s JOIN station st ON st.id=s.station_id WHERE s.id=%s", (s2,)).fetchone()["lat"]
    assert abs(g1 - 38.5) < 1e-6 and abs(g2 - 39.0) < 1e-6      # 2 samples, 2 distinct points


def test_ceden_station_links(conn):
    from fhab.labtasks import link_sample_stations, sample_geo, unlink_sample_station
    staff = _staff(conn)
    conn.execute("INSERT INTO station_registry (station_code, station_name, latitude, longitude) "
                 "VALUES ('CED1','Ceden One',38.5,-121.401),('CED2','Ceden Two',38.5,-121.402)")
    sid = _orphan_sample(conn, "CEDSAMP")     # station at (-121.4, 38.5)
    conn.commit()
    g = sample_geo(conn, sid)
    assert {c["code"] for c in g["ceden_nearby"]} >= {"CED1", "CED2"}     # nearby CEDEN stations shown

    assert link_sample_stations(conn, staff, sid, ["CED1", "CED2"]) == 2   # a sample links to several
    g2 = sample_geo(conn, sid)
    assert {c["code"] for c in g2["ceden_linked"]} == {"CED1", "CED2"}
    assert all(c["code"] not in ("CED1", "CED2") for c in g2["ceden_nearby"])  # linked drop out of nearby

    unlink_sample_station(conn, staff, sid, "CED1")
    assert {c["code"] for c in sample_geo(conn, sid)["ceden_linked"]} == {"CED2"}


def test_ceden_link_web(client, conn):
    conn.execute("INSERT INTO station_registry (station_code, latitude, longitude) "
                 "VALUES ('WEBCED', 38.5, -121.401)")
    sid = _orphan_sample(conn, "WEBCEDSAMP"); conn.commit()
    client.post("/login", data={"email": "staff@wb.ca.gov", "password": "pw"}, follow_redirects=True)
    client.post(f"/lab/sample/{sid}/link-stations", data={"station_code": "WEBCED"}, follow_redirects=True)
    assert conn.execute("SELECT count(*) c FROM sample_station_link WHERE sample_id=%s",
                        (sid,)).fetchone()["c"] == 1
    # geo.json exposes it as a linked location
    j = client.get(f"/lab/sample/{sid}/geo.json").get_json()
    assert any(c["code"] == "WEBCED" for c in j["ceden_linked"])


def test_workboard_sampling_event_search(conn):
    _staff(conn)
    bid = conn.execute("INSERT INTO lab_batch (kind, source) VALUES ('ingested','Clear Lake (RB5)') RETURNING id").fetchone()["id"]
    st = conn.execute("INSERT INTO station (station_code) VALUES ('SE-ST') RETURNING id").fetchone()["id"]
    sid = conn.execute("INSERT INTO sample (station_id, lab_batch_id) VALUES (%s,%s) RETURNING id", (st, bid)).fetchone()["id"]
    conn.execute("INSERT INTO result (result_id_unique, sample_id, data_type) VALUES ('se1',%s,'Laboratory')", (sid,))
    # a second sample with no sampling event
    st2 = conn.execute("INSERT INTO station (station_code) VALUES ('NOEV') RETURNING id").fetchone()["id"]
    sid2 = conn.execute("INSERT INTO sample (station_id) VALUES (%s) RETURNING id", (st2,)).fetchone()["id"]
    conn.execute("INSERT INTO result (result_id_unique, sample_id, data_type) VALUES ('se2',%s,'Laboratory')", (sid2,))
    conn.commit()

    rows = workboard(conn, {})
    assert next(r for r in rows if r["id"] == sid)["event_id"] == bid       # sampling event surfaced

    by_id = {r["id"] for r in workboard(conn, {"event": str(bid)})}         # search by event ID
    assert sid in by_id and sid2 not in by_id
    by_name = {r["id"] for r in workboard(conn, {"event": "Clear Lake"})}    # search by event name
    assert sid in by_name and sid2 not in by_name


def test_orphan_geojson_carries_sampling_event(client, conn):
    bid = conn.execute("INSERT INTO lab_batch (kind, source) VALUES ('ingested','Ev Lake') RETURNING id").fetchone()["id"]
    st = conn.execute("INSERT INTO station (station_code, geom) VALUES ('ORPHEV', "
                      "ST_SetSRID(ST_MakePoint(-121.4,38.5),4326)) RETURNING id").fetchone()["id"]
    sid = conn.execute("INSERT INTO sample (station_id, lab_batch_id, sample_date) "
                       "VALUES (%s,%s,current_date) RETURNING id", (st, bid)).fetchone()["id"]
    conn.execute("INSERT INTO result (result_id_unique, sample_id, data_type) VALUES ('oev',%s,'Laboratory')", (sid,)); conn.commit()
    client.post("/login", data={"email": "staff@wb.ca.gov", "password": "pw"}, follow_redirects=True)
    j = client.get("/api/reports.geojson?data=orphan").get_json()
    feat = next(f for f in j["features"] if f["properties"]["station_code"] == "ORPHEV")
    assert feat["properties"]["event_ids"] == [bid]     # reconcile can scope to this sampling event


def test_routine_geojson_mode(client, conn):
    bid = conn.execute("INSERT INTO lab_batch (kind, source) VALUES ('ingested','R') RETURNING id").fetchone()["id"]
    st = conn.execute("INSERT INTO station (station_code, geom) VALUES ('ROUTST', "
                      "ST_SetSRID(ST_MakePoint(-121.4,38.5),4326)) RETURNING id").fetchone()["id"]
    rsid = conn.execute("INSERT INTO sample (station_id, lab_batch_id, sampling_type, sample_date) "
                        "VALUES (%s,%s,'routine',current_date) RETURNING id", (st, bid)).fetchone()["id"]
    conn.execute("INSERT INTO result (result_id_unique, sample_id, data_type) VALUES ('rt',%s,'Laboratory')", (rsid,))
    # a plain unlinked (non-routine) sample at another station
    st2 = conn.execute("INSERT INTO station (station_code, geom) VALUES ('PLAINST', "
                       "ST_SetSRID(ST_MakePoint(-121.5,38.6),4326)) RETURNING id").fetchone()["id"]
    psid = conn.execute("INSERT INTO sample (station_id, sample_date) VALUES (%s,current_date) RETURNING id", (st2,)).fetchone()["id"]
    conn.execute("INSERT INTO result (result_id_unique, sample_id, data_type) VALUES ('pl',%s,'Laboratory')", (psid,)); conn.commit()
    client.post("/login", data={"email": "staff@wb.ca.gov", "password": "pw"}, follow_redirects=True)

    routine = client.get("/api/reports.geojson?data=routine").get_json()
    codes = {f["properties"]["station_code"] for f in routine["features"]}
    assert "ROUTST" in codes and "PLAINST" not in codes           # only routine locations
    feat = next(f for f in routine["features"] if f["properties"]["station_code"] == "ROUTST")
    assert feat["properties"]["kind"] == "routine" and feat["properties"]["event_ids"] == [bid]
    # routine samples stay OUT of the geocoded-unlinked (orphan) mode
    orphan = client.get("/api/reports.geojson?data=orphan").get_json()
    assert "ROUTST" not in {f["properties"]["station_code"] for f in orphan["features"]}


def test_workboard_requires_staff(client, conn):
    pub = create_user(conn, "v@public.org"); grant_role(conn, pub, "public")
    set_password(conn, pub, "pw")
    client.post("/login", data={"email": "v@public.org", "password": "pw"}, follow_redirects=True)
    assert b"Staff access required" in client.get("/lab/workboard", follow_redirects=True).data
