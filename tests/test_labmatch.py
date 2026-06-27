"""Tests for lab-batch reconciliation (stage a CEDEN chemistry template, fuzzy-match, link)."""

import csv
import datetime

from fhab import labmatch
from fhab.auth import create_user, grant_role
from fhab.reports import enter_report

R5 = "Region 5 - Central Valley"

# Only the columns the parser reads (a real template has 36; DictReader tolerates a subset).
CHEM_COLS = ["LabSampleID", "StationCode", "LocationCode", "Sample Date", "CollectionTime",
             "SampleTypeCode", "Replicate", "ProjectCode", "AgencyCode", "LabBatch",
             "MethodName", "AnalyteName", "FractionName", "UnitName", "Result", "ResQualCode",
             "MDL", "RL", "QACode", "DilutionFactor", "LabResultComments"]


def _write_chem(path, rows):
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CHEM_COLS)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in CHEM_COLS})


def _staff(conn, email="lab@wb.ca.gov"):
    u = create_user(conn, email)
    grant_role(conn, u, "wb_staff", region=R5)
    return u


def test_stage_candidates_and_link(conn, tmp_path):
    staff = _staff(conn)
    conn.execute("""INSERT INTO station_registry (station_code, station_name, latitude, longitude)
                    VALUES ('AAA001','Clear Lake near Dam', 39.0, -122.8)""")
    conn.commit()
    brid = enter_report(conn, staff, water_body_name="Clear Lake", region=R5, lat=39.0, lon=-122.8,
                        observation_date=datetime.date(2018, 7, 12))
    csvp = tmp_path / "chem.csv"
    _write_chem(csvp, [
        {"StationCode": "AAA001", "Sample Date": "2018-07-12", "AnalyteName": "Microcystins",
         "MethodName": "ELISA", "UnitName": "ug/L", "Result": "1.2", "LabBatch": "B1"},
        {"StationCode": "AAA001", "Sample Date": "2018-07-12", "AnalyteName": "Anatoxin-a",
         "MethodName": "ELISA", "UnitName": "ug/L", "Result": "ND", "ResQualCode": "ND"},
        {"StationCode": "BBB002", "Sample Date": "2018-08-21", "AnalyteName": "Microcystins",
         "MethodName": "ELISA", "UnitName": "ug/L", "Result": "5.0"},
    ])
    bid = labmatch.stage_batch(conn, staff, str(csvp), filename="chem.csv")
    b = conn.execute("SELECT n_groups, n_results FROM lab_batch WHERE id=%s", (bid,)).fetchone()
    assert (b["n_groups"], b["n_results"]) == (2, 3)

    sid_a = conn.execute(
        "SELECT id FROM lab_stage_sample WHERE batch_id=%s AND station_code='AAA001'",
        (bid,)).fetchone()["id"]
    cands = labmatch.candidates_for(conn, staff, sid_a, radius_m=2000, days=14)
    assert cands and cands[0]["bloom_report_id"] == brid
    assert cands[0]["dist_m"] < 50 and cands[0]["day_gap"] == 0 and cands[0]["score"] > 0.7

    labmatch.link_stage_sample(conn, staff, sid_a, bloom_report_id=brid)
    assert conn.execute("SELECT status FROM lab_stage_sample WHERE id=%s",
                        (sid_a,)).fetchone()["status"] == "linked"
    n = conn.execute("""SELECT count(*) c FROM result r JOIN sample s ON s.id=r.sample_id
                        WHERE s.bloom_report_id=%s""", (brid,)).fetchone()["c"]
    assert n == 2


def test_create_event_from_stage(conn, tmp_path):
    staff = _staff(conn, "lab2@wb.ca.gov")
    conn.execute("""INSERT INTO station_registry (station_code, station_name, latitude, longitude)
                    VALUES ('CCC003','Lonely Pond', 38.5, -121.5)""")
    conn.commit()
    csvp = tmp_path / "c.csv"
    _write_chem(csvp, [{"StationCode": "CCC003", "Sample Date": "2018-09-01",
                        "AnalyteName": "Microcystins", "MethodName": "ELISA",
                        "Result": "2.0", "UnitName": "ug/L"}])
    bid = labmatch.stage_batch(conn, staff, str(csvp))
    sid = conn.execute("SELECT id FROM lab_stage_sample WHERE batch_id=%s", (bid,)).fetchone()["id"]
    brid = labmatch.create_event_from_stage(conn, staff, sid, region=R5)
    ev = conn.execute("SELECT observation_date FROM event WHERE bloom_report_id=%s",
                      (brid,)).fetchone()
    assert str(ev["observation_date"]) == "2018-09-01"
    assert conn.execute("SELECT status FROM lab_stage_sample WHERE id=%s",
                        (sid,)).fetchone()["status"] == "linked"


def test_auto_match_links_confident_only(conn, tmp_path):
    staff = _staff(conn, "lab3@wb.ca.gov")
    conn.execute("INSERT INTO station_registry (station_code, latitude, longitude) VALUES ('DDD004', 40.0, -120.0)")
    conn.commit()
    brid = enter_report(conn, staff, water_body_name="Auto Lake", region=R5, lat=40.0, lon=-120.0,
                        observation_date=datetime.date(2018, 7, 1))
    csvp = tmp_path / "a.csv"
    _write_chem(csvp, [
        {"StationCode": "DDD004", "Sample Date": "2018-07-01", "AnalyteName": "Microcystins",
         "MethodName": "ELISA", "Result": "3.0"},
        # Far station, no candidate -> must remain unmatched.
        {"StationCode": "EEE005", "Sample Date": "2018-07-01", "AnalyteName": "Microcystins",
         "MethodName": "ELISA", "Result": "1.0"},
    ])
    bid = labmatch.stage_batch(conn, staff, str(csvp))
    n = labmatch.auto_match(conn, staff, bid)
    assert n == 1
    linked = conn.execute(
        "SELECT linked_event FROM lab_stage_sample WHERE batch_id=%s AND status='linked'",
        (bid,)).fetchone()
    assert linked["linked_event"] == brid
    assert conn.execute(
        "SELECT count(*) c FROM lab_stage_sample WHERE batch_id=%s AND status='unmatched'",
        (bid,)).fetchone()["c"] == 1
