"""Lab-data reconciliation workboard: a task-manager layer over lab samples.

Lifecycle of one lab sample's link to an event/report/case:
    unlinked  ->  (assigned)  ->  linked  ->  qa_approved
                                     ^             |
                                     +-- flagged <-+   (revisit / re-reconcile)

Staff (the task manager) assign samples to team members; assignees link each sample to a report
or case (or create a report from its station); a reviewer approves or flags the link. Re-linking
a sample clears its QA so it is reviewed again. This complements batch ingest (lab-reconcile),
which brings *new* CEDEN data in; the workboard manages already-materialized samples.
"""

from __future__ import annotations

import re

import psycopg

from .auth import acting_as
from .reports import enter_report

WORKBOARD_SORTS = {"date": "s.sample_date", "station": "st.station_code", "status": "status"}

_STATUS = """CASE
        WHEN s.qa_status = 'flagged' THEN 'flagged'
        WHEN s.qa_status = 'approved' THEN 'approved'
        WHEN s.bloom_report_id IS NOT NULL OR s.case_id IS NOT NULL THEN 'linked'
        WHEN s.sampling_type = 'routine' THEN 'routine'
        ELSE 'unlinked' END"""

_FROM = """
  FROM sample s
  LEFT JOIN station st ON st.id = s.station_id
  LEFT JOIN event e ON e.bloom_report_id = s.bloom_report_id
  LEFT JOIN location l ON l.id = e.location_id
  LEFT JOIN waterbody w ON w.id = l.waterbody_id
  LEFT JOIN app_user au ON au.id = s.assigned_to
  WHERE EXISTS (SELECT 1 FROM result r WHERE r.sample_id = s.id)
"""


def _where(f: dict, me: int | None):
    cond, p = [], {}
    st = f.get("status")
    if st in ("unlinked", "linked", "approved", "flagged", "routine"):
        cond.append(f"({_STATUS}) = %(status)s"); p["status"] = st
    assignee = f.get("assignee")
    if assignee == "unassigned":
        cond.append("s.assigned_to IS NULL")
    elif assignee == "me" and me:
        cond.append("s.assigned_to = %(me)s"); p["me"] = me
    elif assignee and assignee.isdigit():
        cond.append("s.assigned_to = %(assignee)s"); p["assignee"] = int(assignee)
    if f.get("region"):
        cond.append("w.regional_water_board = %(region)s"); p["region"] = f["region"]
    if str(f.get("batch") or "").isdigit():
        cond.append("s.lab_batch_id = %(batch)s"); p["batch"] = int(f["batch"])
    if f.get("q"):
        cond.append("(st.station_code ILIKE %(q)s OR w.water_body_name ILIKE %(q)s)")
        p["q"] = "%" + f["q"] + "%"
    return (" AND " + " AND ".join(cond)) if cond else "", p


def workboard(conn, f: dict, *, me=None, sort="date", desc=True, limit=100, offset=0) -> list:
    extra, p = _where(f, me)
    col = WORKBOARD_SORTS.get(sort, WORKBOARD_SORTS["date"])
    p["limit"], p["offset"] = limit, offset
    return conn.execute(
        f"""SELECT s.id, st.station_code, st.station_name, s.sample_date,
                   s.bloom_report_id, s.case_id, w.water_body_name, w.regional_water_board,
                   au.email AS assignee, s.qa_status, s.qa_note, ({_STATUS}) AS status,
                   (SELECT count(*) FROM result r WHERE r.sample_id = s.id) AS n_results,
                   COALESCE(ST_Y(st.geom), ST_Y(l.geom)) AS lat,
                   COALESCE(ST_X(st.geom), ST_X(l.geom)) AS lon
            {_FROM}{extra}
            ORDER BY {col} {'DESC' if desc else 'ASC'} NULLS LAST, s.id DESC
            LIMIT %(limit)s OFFSET %(offset)s""", p).fetchall()


def count_workboard(conn, f: dict, *, me=None) -> int:
    extra, p = _where(f, me)
    return conn.execute(f"SELECT count(*) AS c{_FROM}{extra}", p).fetchone()["c"]


def status_tallies(conn) -> dict:
    """Counts per status across all samples with results (for the board's summary chips)."""
    rows = conn.execute(
        f"SELECT ({_STATUS}) AS status, count(*) AS c{_FROM} GROUP BY 1").fetchall()
    return {r["status"]: r["c"] for r in rows}


def team_members(conn) -> list:
    """Active internal-staff users (assignee options)."""
    return conn.execute(
        """SELECT DISTINCT u.id, u.email, u.full_name FROM app_user u
           JOIN user_role ur ON ur.user_id = u.id JOIN role r ON r.code = ur.role_code
           WHERE r.category = 'internal_staff' AND u.is_active ORDER BY u.email""").fetchall()


# ---------- actions (run as the acting user, under RLS) ----------

def assign_samples(conn, user_id, sample_ids, assignee_id) -> int:
    if not sample_ids:
        return 0
    with acting_as(conn, user_id):
        n = conn.execute("UPDATE sample SET assigned_to = %s WHERE id = ANY(%s)",
                         (assignee_id, list(sample_ids))).rowcount
        conn.commit()
    return n


def link_sample(conn, user_id, sample_id, *, bloom_report_id=None, case_id=None) -> None:
    """Link (or re-link) a materialized sample to a report/case. Clears QA so it's re-reviewed."""
    with acting_as(conn, user_id):
        conn.execute(
            """UPDATE sample SET bloom_report_id = %s, case_id = %s,
                   qa_status = NULL, qa_by = NULL, qa_at = NULL WHERE id = %s""",
            (bloom_report_id, case_id, sample_id))
        conn.commit()


def unlink_sample(conn, user_id, sample_id) -> None:
    """Detach a sample from its report/case (back to unlinked), clearing QA."""
    with acting_as(conn, user_id):
        conn.execute(
            """UPDATE sample SET bloom_report_id = NULL, case_id = NULL,
                   qa_status = NULL, qa_by = NULL, qa_at = NULL WHERE id = %s""", (sample_id,))
        conn.commit()


def qa_review(conn, user_id, sample_id, *, approve: bool, note=None) -> None:
    with acting_as(conn, user_id):
        conn.execute(
            """UPDATE sample SET qa_status = %s, qa_by = %s, qa_at = now(), qa_note = %s
               WHERE id = %s""",
            ("approved" if approve else "flagged", user_id, note, sample_id))
        conn.commit()


def tag_routine(conn, user_id, sample_id, *, subtype=None) -> None:
    """Tag a sample as routine monitoring (not tied to a bloom report/case).

    Clears any report/case link so it resolves cleanly out of the unlinked queue. `subtype`
    is reserved for the routine-sampling subtypes to be defined later.
    """
    with acting_as(conn, user_id):
        conn.execute(
            """UPDATE sample SET sampling_type = 'routine', routine_subtype = %s,
                   bloom_report_id = NULL, case_id = NULL WHERE id = %s""",
            (subtype, sample_id))
        conn.commit()


def clear_routine(conn, user_id, sample_id) -> None:
    """Untag a routine sample (back to unlinked)."""
    with acting_as(conn, user_id):
        conn.execute(
            "UPDATE sample SET sampling_type = NULL, routine_subtype = NULL WHERE id = %s",
            (sample_id,))
        conn.commit()


def _best_match(conn, sample_id, radius_m, days):
    """Best candidate event for an unlinked, geocoded sample: confident + unambiguous, else None."""
    s = conn.execute(
        """SELECT s.sample_date, ST_Y(st.geom) AS lat, ST_X(st.geom) AS lon
           FROM sample s JOIN station st ON st.id = s.station_id
           WHERE s.id = %s AND s.bloom_report_id IS NULL AND s.case_id IS NULL
             AND st.geom IS NOT NULL AND s.sample_date IS NOT NULL""", (sample_id,)).fetchone()
    if not s or s["lat"] is None:
        return None
    pt = "ST_SetSRID(ST_MakePoint(%(lon)s,%(lat)s),4326)::geography"
    rows = conn.execute(
        f"""SELECT e.bloom_report_id, ST_Distance(l.geom::geography, {pt}) AS dist_m,
                   abs(e.observation_date - %(d)s) AS gap
            FROM event e JOIN location l ON l.id = e.location_id
            WHERE l.geom IS NOT NULL AND e.observation_date IS NOT NULL
              AND ST_DWithin(l.geom::geography, {pt}, %(r)s)
              AND e.observation_date BETWEEN %(d)s::date - %(days)s::int AND %(d)s::date + %(days)s::int
            ORDER BY dist_m LIMIT 2""",
        {"lon": s["lon"], "lat": s["lat"], "d": s["sample_date"], "r": radius_m, "days": days}
    ).fetchall()
    if not rows:
        return None

    def score(r):
        sp = max(0.0, 1 - r["dist_m"] / radius_m) if radius_m else 0.0
        tp = max(0.0, 1 - r["gap"] / days) if days else 0.0
        return 0.6 * sp + 0.4 * tp

    scored = sorted(((score(r), r["bloom_report_id"]) for r in rows), reverse=True)
    if scored[0][0] < 0.6:                              # not close enough in space+time
        return None
    if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.1:   # two plausible matches — ambiguous
        return None
    return scored[0][1]


def batch_reconcile_samples(conn, sample_ids, *, radius_m=8000, days=14) -> dict:
    """Auto-link each unlinked, geocoded sample to its best-matching event (within distance +
    the ±days window) when the match is confident and unambiguous. Skips the rest for manual
    review. Runs on the privileged connection (staff-only bulk action). Returns counts.
    """
    linked = skipped = 0
    for sid in sample_ids:
        brid = _best_match(conn, sid, radius_m, days)
        if brid is None:
            skipped += 1
            continue
        conn.execute(
            """UPDATE sample SET bloom_report_id = %s, qa_status = NULL, qa_by = NULL, qa_at = NULL
               WHERE id = %s AND bloom_report_id IS NULL AND case_id IS NULL""", (brid, sid))
        linked += 1
    conn.commit()
    return {"linked": linked, "skipped": skipped}


def sample_geo(conn, sample_id, *, radius_m=8000, limit=8, at=None) -> dict:
    """Geospatial context for one sample: its station, its linked event, and nearby candidate
    reports (events within `radius_m` of an anchor point). For the workboard's map.

    `at=(lat, lon)` overrides the anchor with a typed/searched point (returned as `probe`), so
    an ungeocoded sample can still find nearby reports/cases around coordinates read off the CoC.
    """
    s = conn.execute(
        """SELECT s.sample_date, s.bloom_report_id, st.station_code, st.station_name,
                  ST_Y(st.geom) AS st_lat, ST_X(st.geom) AS st_lon
           FROM sample s LEFT JOIN station st ON st.id = s.station_id WHERE s.id = %s""",
        (sample_id,)).fetchone()
    out = {"label": None, "sample_date": None, "station": None, "linked": None,
           "candidates": [], "probe": None}
    if not s:
        return out
    out["label"] = f"{s['station_code'] or 'sample'} · {s['sample_date'] or '—'}"
    out["sample_date"] = str(s["sample_date"]) if s["sample_date"] else None
    if s["st_lat"] is not None:
        out["station"] = {"lat": s["st_lat"], "lon": s["st_lon"],
                          "code": s["station_code"], "name": s["station_name"]}
    if at is not None:
        out["probe"] = {"lat": float(at[0]), "lon": float(at[1])}

    if s["bloom_report_id"]:
        ev = conn.execute(
            """SELECT ST_Y(l.geom) AS lat, ST_X(l.geom) AS lon, w.water_body_name,
                      e.observation_date::text AS obs
               FROM event e JOIN location l ON l.id = e.location_id
               LEFT JOIN waterbody w ON w.id = l.waterbody_id
               WHERE e.bloom_report_id = %s AND l.geom IS NOT NULL""",
            (s["bloom_report_id"],)).fetchone()
        if ev and ev["lat"] is not None:
            out["linked"] = {"lat": ev["lat"], "lon": ev["lon"], "brid": s["bloom_report_id"],
                             "name": ev["water_body_name"], "obs": ev["obs"]}

    # Anchor for "nearby" = the searched probe point, else the station, else the linked event.
    anchor = out["probe"] or out["station"] or out["linked"]
    if anchor:
        rows = conn.execute(
            """SELECT e.bloom_report_id, e.case_id, w.water_body_name,
                      e.observation_date::text AS obs,
                      ST_Y(l.geom) AS lat, ST_X(l.geom) AS lon,
                      round(ST_Distance(l.geom::geography,
                            ST_SetSRID(ST_MakePoint(%(lon)s,%(lat)s),4326)::geography)) AS dist_m,
                      abs(e.observation_date - %(d)s) AS day_gap
               FROM event e JOIN location l ON l.id = e.location_id
               LEFT JOIN waterbody w ON w.id = l.waterbody_id
               WHERE l.geom IS NOT NULL
                 AND ST_DWithin(l.geom::geography,
                       ST_SetSRID(ST_MakePoint(%(lon)s,%(lat)s),4326)::geography, %(r)s)
                 AND e.bloom_report_id IS DISTINCT FROM %(linked)s
               ORDER BY dist_m LIMIT %(lim)s""",
            {"lon": anchor["lon"], "lat": anchor["lat"], "d": s["sample_date"], "r": radius_m,
             "linked": s["bloom_report_id"], "lim": limit}).fetchall()
        out["candidates"] = [
            {"brid": r["bloom_report_id"], "case_id": r["case_id"], "name": r["water_body_name"],
             "obs": r["obs"], "lat": r["lat"], "lon": r["lon"], "dist_m": r["dist_m"],
             "day_gap": r["day_gap"]} for r in rows]
    return out


def set_sample_location(conn, sample_id, lat: float, lon: float) -> dict:
    """Set the geographic location of a sample's station from typed/OCR'd coordinates.

    Used for samples whose station is not in the CEDEN registry (coordinates live on the
    chain-of-custody form). Updates the station geom so the sample becomes geocoded — it then
    appears on the main map and is eligible for batch auto-reconcile. Creates a station for the
    sample if it somehow has none. Returns {station_id, lat, lon}.
    """
    lat, lon = float(lat), float(lon)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("Latitude must be -90..90 and longitude -180..180.")
    row = conn.execute("SELECT station_id FROM sample WHERE id=%s", (sample_id,)).fetchone()
    if not row:
        raise ValueError("Sample not found.")
    station_id = row["station_id"]
    if station_id is None:
        station_id = conn.execute(
            "INSERT INTO station (station_code) VALUES (%s) RETURNING id",
            (f"SAMPLE-{sample_id}",)).fetchone()["id"]
        conn.execute("UPDATE sample SET station_id=%s WHERE id=%s", (station_id, sample_id))
    conn.execute(
        """UPDATE station
           SET geom = ST_SetSRID(ST_MakePoint(%s, %s), 4326), datum = COALESCE(datum, 'WGS84')
           WHERE id = %s""", (lon, lat, station_id))
    conn.commit()
    return {"station_id": station_id, "lat": lat, "lon": lon}


# Each line: a station code (or sample id) followed by latitude then longitude. The key may
# contain spaces and commas (e.g. "El Dorado E. RP, North Pond") so we anchor on the two
# trailing numeric tokens and treat everything before them as the key.
_COORD_LINE = re.compile(
    r"^(?P<key>.*?)[\s,]+(?P<lat>-?\d{1,3}(?:\.\d+)?)[\s,]+(?P<lon>-?\d{1,3}(?:\.\d+)?)\s*$")


def parse_coord_rows(text: str) -> list[dict]:
    """Parse pasted 'station, lat, lon' lines into {key, lat, lon} rows (status='parsed'|'unparsed')."""
    rows = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.lower().startswith(("station", "#", "key")):
            continue
        m = _COORD_LINE.match(line)
        if not m:
            rows.append({"raw": line, "status": "unparsed"}); continue
        rows.append({"key": m.group("key").strip().strip(",").strip(),
                     "lat": float(m.group("lat")), "lon": float(m.group("lon")),
                     "status": "parsed"})
    return rows


def bulk_geocode(conn, user_id, text: str) -> dict:
    """Geocode many samples at once from pasted 'station_code|sample_id, lat, lon' lines.

    Matches each key against station_code (geocoding every sample at that station), or a numeric
    sample id. Returns {applied, samples, rows:[...with per-row status]}.
    """
    rows = parse_coord_rows(text)
    applied = samples = 0
    for r in rows:
        if r["status"] != "parsed":
            continue
        if not (-90 <= r["lat"] <= 90 and -180 <= r["lon"] <= 180):
            r["status"] = "out of range"; continue
        hit = conn.execute(
            """UPDATE station SET geom = ST_SetSRID(ST_MakePoint(%s, %s), 4326),
                   datum = COALESCE(datum, 'WGS84') WHERE station_code = %s RETURNING id""",
            (r["lon"], r["lat"], r["key"])).fetchall()
        if hit:
            n = conn.execute("SELECT count(*) c FROM sample WHERE station_id = %s", (hit[0]["id"],)).fetchone()["c"]
            r["status"] = f"✓ station · {n} sample(s)"; applied += 1; samples += n; continue
        if r["key"].isdigit() and conn.execute("SELECT 1 FROM sample WHERE id=%s", (int(r["key"]),)).fetchone():
            set_sample_location(conn, int(r["key"]), r["lat"], r["lon"])
            r["status"] = "✓ sample"; applied += 1; samples += 1; continue
        r["status"] = "no matching station / sample"
    conn.commit()
    return {"applied": applied, "samples": samples, "rows": rows}


def create_report_from_sample(conn, user_id, sample_id, *, region=None) -> int:
    """Create a report from an unlinked sample's station + date, then link the sample to it."""
    info = conn.execute(
        """SELECT s.sample_date, st.station_code, st.station_name,
                  ST_Y(st.geom) AS lat, ST_X(st.geom) AS lon
           FROM sample s LEFT JOIN station st ON st.id = s.station_id WHERE s.id = %s""",
        (sample_id,)).fetchone()
    name = info["station_name"] or info["station_code"] or "Lab sample site"
    brid = enter_report(conn, user_id, water_body_name=name, region=region,
                        lat=info["lat"], lon=info["lon"], observation_date=info["sample_date"],
                        report_type="Lab data",
                        description=f"Created from unlinked lab sample (station {info['station_code']}).")
    link_sample(conn, user_id, sample_id, bloom_report_id=brid)
    return brid
