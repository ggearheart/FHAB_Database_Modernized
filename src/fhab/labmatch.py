"""Lab-batch reconciliation: stage a full CEDEN chemistry template, fuzzy-match each
station+date sample group to FHAB events/reports/cases, and let wb_staff confirm.

The official CEDEN chemistry template (36 cols, no BG_ID) links to events only by StationCode
+ Sample Date, so this never auto-attaches on upload. Instead it stages the file, scores
candidate reports (spatial + temporal + name), and materializes a group into live
sample/result rows only when a staffer (or a high-confidence auto-match) confirms a link.
"""

from __future__ import annotations

import csv
import difflib
from pathlib import Path

import psycopg

from .auth import acting_as
from .ceden import CedenLoader, _parse_time
from .parsing import clean, parse_date, parse_float
from .reports import enter_report

# Composite-score weights (sum to 1.0).
W_SPATIAL, W_TEMPORAL, W_NAME = 0.5, 0.3, 0.2
# Auto-match thresholds: accept only confident, unambiguous top candidates.
AUTO_MIN_SCORE, AUTO_MARGIN = 0.75, 0.10


def _ceden_rows(path):
    with Path(path).open(newline="", encoding="utf-8-sig") as fh:
        yield from csv.DictReader(fh)


def _resolve_station(conn, code):
    """Resolve a StationCode to a station id (geocoding from the registry when possible)."""
    code = clean(code)
    if not code:
        return None
    row = conn.execute("SELECT id FROM station WHERE station_code = %s", (code,)).fetchone()
    if row:
        return row["id"]
    reg = conn.execute(
        "SELECT station_name, latitude, longitude, datum FROM station_registry WHERE station_code = %s",
        (code,)).fetchone()
    if reg and reg["latitude"] is not None and reg["longitude"] is not None:
        return conn.execute(
            """INSERT INTO station (station_code, station_name, geom, datum)
               VALUES (%s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326), %s)
               ON CONFLICT (station_code) DO UPDATE
                 SET station_name = COALESCE(EXCLUDED.station_name, station.station_name)
               RETURNING id""",
            (code, clean(reg["station_name"]), reg["longitude"], reg["latitude"],
             clean(reg["datum"]))).fetchone()["id"]
    # Unknown code: record it (geomless) so it surfaces for manual geocoding.
    return conn.execute(
        """INSERT INTO station (station_code) VALUES (%s)
           ON CONFLICT (station_code) DO UPDATE SET station_code = EXCLUDED.station_code
           RETURNING id""", (code,)).fetchone()["id"]


def stage_batch(conn, user_id, path, *, filename=None, radius_m=2000, days=14) -> int:
    """Parse a CEDEN chemistry template into staging tables (no live event/sample writes)."""
    groups: dict[tuple, dict] = {}
    for row in _ceden_rows(path):
        code = clean(row.get("StationCode"))
        sdate = parse_date(row.get("Sample Date") or row.get("SampleDate"))
        key = (code, str(sdate), clean(row.get("LocationCode")), clean(row.get("Replicate")))
        g = groups.setdefault(key, {
            "station_code": code, "sample_date": sdate,
            "location_code": clean(row.get("LocationCode")), "replicate": clean(row.get("Replicate")),
            "sample_time": _parse_time(row.get("CollectionTime")),
            "sample_type": clean(row.get("SampleTypeCode")),
            "lab_sample_id": clean(row.get("LabSampleID")),
            "lab_batch_code": clean(row.get("LabBatch")),
            "project_code": clean(row.get("ProjectCode")),
            "agency_code": clean(row.get("AgencyCode")), "results": []})
        g["results"].append(row)
    n_results = sum(len(g["results"]) for g in groups.values())
    with acting_as(conn, user_id):
        bid = conn.execute(
            """INSERT INTO lab_batch (filename, uploaded_by, match_radius_m, match_days,
                                      n_groups, n_results)
               VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
            (filename, user_id, radius_m, days, len(groups), n_results)).fetchone()["id"]
        for g in groups.values():
            station_id = _resolve_station(conn, g["station_code"])
            sid = conn.execute(
                """INSERT INTO lab_stage_sample
                     (batch_id, station_code, location_code, replicate, sample_date, sample_time,
                      sample_type, lab_sample_id, lab_batch_code, project_code, agency_code, station_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (bid, g["station_code"], g["location_code"], g["replicate"], g["sample_date"],
                 g["sample_time"], g["sample_type"], g["lab_sample_id"], g["lab_batch_code"],
                 g["project_code"], g["agency_code"], station_id)).fetchone()["id"]
            for r in g["results"]:
                conn.execute(
                    """INSERT INTO lab_stage_result
                         (stage_sample_id, analyte_name, method_name, fraction_name, unit_name,
                          matrix_name, result, res_qual_code, mdl, rl, qa_code, dilution_factor,
                          result_comments)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (sid, clean(r.get("AnalyteName")), clean(r.get("MethodName")),
                     clean(r.get("FractionName")), clean(r.get("UnitName")),
                     clean(r.get("MatrixName")), clean(r.get("Result")),
                     clean(r.get("ResQualCode")), clean(r.get("MDL")), clean(r.get("RL")),
                     clean(r.get("QACode")), clean(r.get("DilutionFactor")),
                     clean(r.get("LabResultComments"))))
        conn.commit()
    return bid


def _candidates(conn, stage_sample_id, *, radius_m, days, limit=5):
    """Score candidate events for one staged group (caller manages acting_as)."""
    s = conn.execute(
        """SELECT ss.sample_date, st.station_name, ST_Y(st.geom) AS lat, ST_X(st.geom) AS lon
           FROM lab_stage_sample ss LEFT JOIN station st ON st.id = ss.station_id
           WHERE ss.id = %s""", (stage_sample_id,)).fetchone()
    if not s or s["lat"] is None or s["sample_date"] is None:
        return []
    pt = "ST_SetSRID(ST_MakePoint(%(lon)s,%(lat)s),4326)::geography"
    rows = conn.execute(
        f"""SELECT e.bloom_report_id, e.case_id, e.observation_date, w.water_body_name,
                   w.regional_water_board, ST_Y(l.geom) AS rep_lat, ST_X(l.geom) AS rep_lon,
                   ST_Distance(l.geom::geography, {pt}) AS dist_m,
                   abs(e.observation_date - %(d)s) AS day_gap
            FROM event e JOIN location l ON l.id = e.location_id
            LEFT JOIN waterbody w ON w.id = l.waterbody_id
            WHERE l.geom IS NOT NULL AND e.observation_date IS NOT NULL
              AND ST_DWithin(l.geom::geography, {pt}, %(r)s)
              AND e.observation_date BETWEEN %(d)s::date - %(days)s AND %(d)s::date + %(days)s
            ORDER BY dist_m LIMIT 50""",
        {"lon": s["lon"], "lat": s["lat"], "d": s["sample_date"], "r": radius_m, "days": days}
    ).fetchall()
    out = []
    for r in rows:
        spatial = max(0.0, 1 - r["dist_m"] / radius_m) if radius_m else 0.0
        temporal = max(0.0, 1 - r["day_gap"] / days) if days else 0.0
        name = difflib.SequenceMatcher(
            None, (s["station_name"] or "").lower(),
            (r["water_body_name"] or "").lower()).ratio()
        score = W_SPATIAL * spatial + W_TEMPORAL * temporal + W_NAME * name
        out.append({"bloom_report_id": r["bloom_report_id"], "case_id": r["case_id"],
                    "water_body_name": r["water_body_name"], "region": r["regional_water_board"],
                    "observation_date": r["observation_date"], "dist_m": round(r["dist_m"]),
                    "day_gap": r["day_gap"], "name_sim": round(name, 2), "score": round(score, 3),
                    "rep_lat": r["rep_lat"], "rep_lon": r["rep_lon"]})
    out.sort(key=lambda c: c["score"], reverse=True)
    return out[:limit]


def candidates_for(conn, user_id, stage_sample_id, *, radius_m, days, limit=5):
    """Public candidate scorer for one staged group, run under RLS as `user_id`."""
    with acting_as(conn, user_id):
        return _candidates(conn, stage_sample_id, radius_m=radius_m, days=days, limit=limit)


def _materialize(conn, user_id, stage_sample_id, *, bloom_report_id=None, case_id=None) -> int:
    """Create a live sample + result rows from a staged group (caller manages acting_as)."""
    loader = CedenLoader(conn)
    ss = conn.execute("SELECT * FROM lab_stage_sample WHERE id = %s", (stage_sample_id,)).fetchone()
    sample_id = conn.execute(
        """INSERT INTO sample
             (bloom_report_id, case_id, station_id, sample_date, sample_time, sample_type,
              lab_sample_id, lab_batch, project_code, lab_agency_code)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (bloom_report_id, case_id, ss["station_id"], ss["sample_date"], ss["sample_time"],
         ss["sample_type"], ss["lab_sample_id"], ss["lab_batch_code"], ss["project_code"],
         ss["agency_code"])).fetchone()["id"]
    for r in conn.execute(
            "SELECT * FROM lab_stage_result WHERE stage_sample_id = %s", (stage_sample_id,)).fetchall():
        analyte_id = loader._analyte_id(r["analyte_name"], r["method_name"])
        conn.execute(
            """INSERT INTO result
                 (result_id_unique, sample_id, analyte_id, data_type, method, measurement_value,
                  measurement_unit, matrix_name, res_qual_code, fraction_name, mdl, rl)
               VALUES (%s,%s,%s,'Laboratory',%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (result_id_unique) DO UPDATE SET
                 measurement_value = EXCLUDED.measurement_value""",
            (f"labstage:{stage_sample_id}:{r['id']}", sample_id, analyte_id, r["method_name"],
             parse_float(r["result"]), r["unit_name"], r["matrix_name"], r["res_qual_code"],
             r["fraction_name"], parse_float(r["mdl"]), parse_float(r["rl"])))
    conn.execute(
        """UPDATE lab_stage_sample SET status='linked', linked_event=%s, linked_case=%s,
               linked_sample=%s, decided_by=%s, decided_at=now() WHERE id=%s""",
        (bloom_report_id, case_id, sample_id, user_id, stage_sample_id))
    return sample_id


def link_stage_sample(conn, user_id, stage_sample_id, *, bloom_report_id=None, case_id=None) -> int:
    """Materialize a staged group and pin it to an event/case (one staff decision)."""
    with acting_as(conn, user_id):
        sid = _materialize(conn, user_id, stage_sample_id,
                           bloom_report_id=bloom_report_id, case_id=case_id)
        conn.commit()
    return sid


def skip_stage_sample(conn, user_id, stage_sample_id) -> None:
    with acting_as(conn, user_id):
        conn.execute(
            "UPDATE lab_stage_sample SET status='skipped', decided_by=%s, decided_at=now() WHERE id=%s",
            (user_id, stage_sample_id))
        conn.commit()


def create_event_from_stage(conn, user_id, stage_sample_id, *, region=None) -> int:
    """Create a new report/event from a staged group's station + date, then link the lab data."""
    info = conn.execute(
        """SELECT ss.sample_date, ss.station_code, st.station_name,
                  ST_Y(st.geom) AS lat, ST_X(st.geom) AS lon
           FROM lab_stage_sample ss LEFT JOIN station st ON st.id = ss.station_id
           WHERE ss.id = %s""", (stage_sample_id,)).fetchone()
    name = info["station_name"] or info["station_code"] or "Lab sample site"
    brid = enter_report(conn, user_id, water_body_name=name, region=region,
                        lat=info["lat"], lon=info["lon"], observation_date=info["sample_date"],
                        report_type="Lab batch",
                        description=f"Created from lab batch (station {info['station_code']}).")
    link_stage_sample(conn, user_id, stage_sample_id, bloom_report_id=brid)
    return brid


def auto_match(conn, user_id, batch_id, *, min_score=AUTO_MIN_SCORE, margin=AUTO_MARGIN) -> int:
    """Link unmatched groups whose top candidate is confident and unambiguous. Returns count."""
    b = conn.execute("SELECT match_radius_m, match_days FROM lab_batch WHERE id=%s",
                     (batch_id,)).fetchone()
    n = 0
    with acting_as(conn, user_id):
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM lab_stage_sample WHERE batch_id=%s AND status='unmatched'",
            (batch_id,)).fetchall()]
        for sid in ids:
            c = _candidates(conn, sid, radius_m=b["match_radius_m"], days=b["match_days"], limit=2)
            if c and c[0]["score"] >= min_score and (len(c) == 1 or c[0]["score"] - c[1]["score"] >= margin):
                _materialize(conn, user_id, sid, bloom_report_id=c[0]["bloom_report_id"],
                             case_id=c[0]["case_id"])
                n += 1
        conn.commit()
    return n
