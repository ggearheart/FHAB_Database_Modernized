"""Sample work area: browse, create, and edit lab sample records.

A sample-centric view over the lab data — every sample, however it arrived (folder/CEDEN ingest,
manual entry), with staff CRUD. Complements the workboard (which is link/QA focused and only shows
samples that have results). Writes use the owner connection like `set_sample_location`.
"""

from __future__ import annotations

from .labtasks import set_sample_point
from .parsing import clean, parse_date

# Sample columns a staffer may edit from the work area (identity + collection metadata).
_EDITABLE = ["sample_type", "collected_by", "site", "coc_id", "bg_id", "lab_sample_id",
             "lab_batch", "project_code", "sample_location", "owner_org"]

_FROM = ("FROM sample s LEFT JOIN station st ON st.id = s.station_id "
         "LEFT JOIN lab_batch b ON b.id = s.lab_batch_id")

_STATUS = ("CASE WHEN s.bloom_report_id IS NOT NULL OR s.case_id IS NOT NULL THEN 'linked' "
           "WHEN s.sampling_type = 'routine' THEN 'routine' ELSE 'unlinked' END")


def _where(f: dict):
    cond, p = [], {}
    if f.get("q"):
        cond.append("(st.station_code ILIKE %(q)s OR st.station_name ILIKE %(q)s OR s.bg_id ILIKE %(q)s "
                    "OR s.lab_sample_id ILIKE %(q)s OR s.sample_id ILIKE %(q)s)")
        p["q"] = "%" + f["q"] + "%"
    if str(f.get("batch") or "").isdigit():
        cond.append("s.lab_batch_id = %(batch)s"); p["batch"] = int(f["batch"])
    st = f.get("status")
    if st in ("linked", "unlinked", "routine"):
        cond.append(f"({_STATUS}) = %(status)s"); p["status"] = st
    geo = f.get("geocoded")
    if geo == "yes":
        cond.append("st.geom IS NOT NULL")
    elif geo == "no":
        cond.append("st.geom IS NULL")
    return (" WHERE " + " AND ".join(cond)) if cond else "", p


def list_samples(conn, f: dict, *, limit=100, offset=0) -> list:
    where, p = _where(f)
    p["limit"], p["offset"] = limit, offset
    return conn.execute(
        f"""SELECT s.id, st.station_code, st.station_name, s.sample_date, s.sample_type, s.bg_id,
                   s.lab_sample_id, s.lab_batch_id, b.source AS batch_source, b.kind AS batch_kind,
                   (st.geom IS NOT NULL) AS geocoded, s.bloom_report_id, s.case_id,
                   ({_STATUS}) AS status,
                   (SELECT count(*) FROM result r WHERE r.sample_id = s.id) AS n_results
            {_FROM}{where} ORDER BY s.id DESC LIMIT %(limit)s OFFSET %(offset)s""", p).fetchall()


def count_samples(conn, f: dict) -> int:
    where, p = _where(f)
    return conn.execute(f"SELECT count(*) AS c {_FROM}{where}", p).fetchone()["c"]


def get_sample(conn, sid: int) -> dict | None:
    s = conn.execute(
        f"""SELECT s.*, st.station_code, st.station_name, ST_Y(st.geom) AS lat, ST_X(st.geom) AS lon,
                   b.source AS batch_source, b.kind AS batch_kind, w.water_body_name,
                   ({_STATUS}) AS status
            FROM sample s LEFT JOIN station st ON st.id = s.station_id
            LEFT JOIN lab_batch b ON b.id = s.lab_batch_id
            LEFT JOIN event e ON e.bloom_report_id = s.bloom_report_id
            LEFT JOIN location l ON l.id = e.location_id
            LEFT JOIN waterbody w ON w.id = l.waterbody_id
            WHERE s.id = %s""", (sid,)).fetchone()
    if not s:
        return None
    results = conn.execute(
        """SELECT a.analyte, r.method, r.measurement_value, r.measurement_text,
                  r.res_qual_code, r.measurement_unit, r.fraction_name
           FROM result r LEFT JOIN analyte a ON a.id = r.analyte_id
           WHERE r.sample_id = %s ORDER BY a.analyte""", (sid,)).fetchall()
    ceden = conn.execute(
        "SELECT station_code, station_name FROM sample_station_link WHERE sample_id=%s ORDER BY station_code",
        (sid,)).fetchall()
    return {"sample": s, "results": results, "ceden": ceden}


def _resolve_station(conn, code, name):
    code = clean(code)
    if not code:
        return None
    return conn.execute(
        """INSERT INTO station (station_code, station_name) VALUES (%s,%s)
           ON CONFLICT (station_code)
             DO UPDATE SET station_name = COALESCE(EXCLUDED.station_name, station.station_name)
           RETURNING id""", (code, clean(name))).fetchone()["id"]


def create_sample(conn, user_id, data: dict) -> int:
    """Create a sample manually. Resolves/creates a station from station_code, geocodes if given."""
    station_id = _resolve_station(conn, data.get("station_code"), data.get("station_name"))
    sid = conn.execute(
        """INSERT INTO sample (station_id, sample_date, sample_time, sample_type, collected_by,
             site, coc_id, bg_id, lab_sample_id, lab_batch, project_code, sample_location, owner_org)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (station_id, parse_date(data.get("sample_date")), clean(data.get("sample_time")) or None,
         clean(data.get("sample_type")), clean(data.get("collected_by")), clean(data.get("site")),
         clean(data.get("coc_id")), clean(data.get("bg_id")), clean(data.get("lab_sample_id")),
         clean(data.get("lab_batch")), clean(data.get("project_code")),
         clean(data.get("sample_location")), clean(data.get("owner_org")))).fetchone()["id"]
    lat, lon = (data.get("lat") or "").strip(), (data.get("lon") or "").strip()
    if lat and lon:
        set_sample_point(conn, sid, lat, lon)
    conn.commit()
    return sid


def update_sample(conn, user_id, sid: int, data: dict) -> None:
    """Update a sample's editable fields, its station name, and/or its coordinates."""
    sets, p = [], {"id": sid}
    for k in _EDITABLE:
        if k in data:
            sets.append(f"{k} = %({k})s"); p[k] = clean(data.get(k))
    if "sample_date" in data:
        sets.append("sample_date = %(sample_date)s"); p["sample_date"] = parse_date(data.get("sample_date"))
    if "sample_time" in data:
        sets.append("sample_time = %(sample_time)s"); p["sample_time"] = clean(data.get("sample_time")) or None
    if sets:
        conn.execute(f"UPDATE sample SET {', '.join(sets)} WHERE id = %(id)s", p)
    if data.get("station_name") is not None:
        conn.execute("UPDATE station st SET station_name = %s FROM sample s "
                     "WHERE s.id = %s AND st.id = s.station_id", (clean(data.get("station_name")), sid))
    lat, lon = (data.get("lat") or "").strip(), (data.get("lon") or "").strip()
    if lat and lon:
        set_sample_point(conn, sid, lat, lon)
    conn.commit()
