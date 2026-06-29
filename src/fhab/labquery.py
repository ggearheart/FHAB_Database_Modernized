"""Cross-report lab/field results browser: filter, sort, and export results across all reports.

Reads run on the privileged connection (the screen is staff-only). Filters and sort columns are
whitelisted; everything else is parameterized.
"""

from __future__ import annotations

import psycopg

# Whitelisted sort keys -> SQL expressions (never interpolate user input as a column).
RESULT_SORTS = {
    "date": "s.sample_date",
    "value": "r.measurement_value",
    "waterbody": "w.water_body_name",
    "analyte": "an.analyte",
}

_BASE = """
  FROM result r
  JOIN sample s ON s.id = r.sample_id
  LEFT JOIN event e ON e.bloom_report_id = s.bloom_report_id
  LEFT JOIN location l ON l.id = e.location_id
  LEFT JOIN waterbody w ON w.id = l.waterbody_id
  LEFT JOIN analyte an ON an.id = r.analyte_id
"""


def _where(f: dict):
    cond, p = [], {}
    if f.get("analysis_type"):
        cond.append("an.analysis_type = %(analysis_type)s"); p["analysis_type"] = f["analysis_type"]
    if f.get("analyte"):
        cond.append("an.analyte = %(analyte)s"); p["analyte"] = f["analyte"]
    if f.get("region"):
        cond.append("w.regional_water_board = %(region)s"); p["region"] = f["region"]
    if f.get("data_type"):
        cond.append("r.data_type::text = %(data_type)s"); p["data_type"] = f["data_type"]
    if f.get("q"):
        cond.append("(w.water_body_name ILIKE %(q)s OR an.analyte ILIKE %(q)s "
                    "OR an.analyte_class ILIKE %(q)s)")
        p["q"] = "%" + f["q"] + "%"
    if f.get("date_from"):
        cond.append("s.sample_date >= %(date_from)s"); p["date_from"] = f["date_from"]
    if f.get("date_to"):
        cond.append("s.sample_date <= %(date_to)s"); p["date_to"] = f["date_to"]
    nd = f.get("nd")
    if nd == "only":
        cond.append("r.res_qual_code = 'ND'")
    elif nd == "exclude":
        cond.append("(r.res_qual_code IS DISTINCT FROM 'ND')")
    where = (" WHERE " + " AND ".join(cond)) if cond else ""
    return where, p


_SELECT = """SELECT s.sample_date, w.water_body_name, w.regional_water_board, w.county,
              e.bloom_report_id, an.analysis_type, an.analyte_class, an.analyte,
              r.data_type::text AS data_type, r.measurement_value, r.measurement_text,
              r.measurement_unit, r.res_qual_code, r.method, r.mdl, r.rl, s.site"""


def query_results(conn: psycopg.Connection, f: dict, *, sort="date", desc=True,
                  limit=100, offset=0) -> list:
    where, p = _where(f)
    col = RESULT_SORTS.get(sort, RESULT_SORTS["date"])
    direction = "DESC" if desc else "ASC"
    sql = (f"{_SELECT}{_BASE}{where} ORDER BY {col} {direction} NULLS LAST, "
           f"s.sample_date DESC NULLS LAST, r.result_id_unique DESC LIMIT %(limit)s OFFSET %(offset)s")
    p["limit"], p["offset"] = limit, offset
    return conn.execute(sql, p).fetchall()


def count_results(conn: psycopg.Connection, f: dict) -> int:
    where, p = _where(f)
    return conn.execute(f"SELECT count(*) AS c{_BASE}{where}", p).fetchone()["c"]


def filter_options(conn: psycopg.Connection) -> dict:
    ats = [r["analysis_type"] for r in conn.execute(
        "SELECT DISTINCT analysis_type FROM analyte WHERE analysis_type IS NOT NULL ORDER BY 1").fetchall()]
    regs = [r["regional_water_board"] for r in conn.execute(
        "SELECT DISTINCT regional_water_board FROM waterbody "
        "WHERE regional_water_board IS NOT NULL ORDER BY 1").fetchall()]
    dts = [r["data_type"] for r in conn.execute(
        "SELECT DISTINCT data_type::text AS data_type FROM result "
        "WHERE data_type IS NOT NULL ORDER BY 1").fetchall()]
    return {"analysis_types": ats, "regions": regs, "data_types": dts}
