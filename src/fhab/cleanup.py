"""Data cleanup: find and remove empty lab-record artifacts.

An "empty" sample is one with **no substantive result** — no result carries a measurement value,
a measurement text, or a qualifier (a non-detect *is* substantive: it has a qualifier). These are
the near-empty shells left by malformed ingest rows. Deletion is guarded: `delete_samples` only ever
removes rows that are genuinely empty (and, by default, unlinked), so a bad request can't wipe real
data; every delete is captured by the audit log.
"""

from __future__ import annotations

import psycopg

# A result is substantive if it has an actual measurement or a qualifier (ND counts).
_HAS_DATA = """EXISTS (SELECT 1 FROM result r WHERE r.sample_id = s.id
    AND (r.measurement_value IS NOT NULL
         OR nullif(btrim(r.measurement_text), '') IS NOT NULL
         OR nullif(btrim(r.res_qual_code), '') IS NOT NULL))"""

_FROM = ("FROM sample s LEFT JOIN station st ON st.id = s.station_id "
         "LEFT JOIN lab_batch b ON b.id = s.lab_batch_id")


# A record has "stray identity" if it carries any of these despite having no data.
_HAS_IDENTITY = ("(st.station_code IS NOT NULL OR s.sample_date IS NOT NULL "
                 "OR nullif(btrim(s.bg_id), '') IS NOT NULL "
                 "OR nullif(btrim(s.lab_sample_id), '') IS NOT NULL)")


def _where(f: dict) -> tuple[str, dict]:
    cond = [f"NOT {_HAS_DATA}"]
    p: dict = {}
    if not f.get("include_linked"):
        cond.append("s.bloom_report_id IS NULL AND s.case_id IS NULL")
    # triage: totally blank (no identifying fields) vs. has a station/date/id but no data
    mode = f.get("mode")
    if mode == "blank":
        cond.append(f"NOT {_HAS_IDENTITY}")
    elif mode == "identified":
        cond.append(_HAS_IDENTITY)
    if str(f.get("batch") or "").isdigit():
        cond.append("s.lab_batch_id = %(batch)s"); p["batch"] = int(f["batch"])
    if f.get("q"):
        cond.append("(st.station_code ILIKE %(q)s OR s.bg_id ILIKE %(q)s OR b.source ILIKE %(q)s)")
        p["q"] = f"%{f['q']}%"
    if f.get("analyte"):
        cond.append("EXISTS (SELECT 1 FROM result r JOIN analyte a ON a.id = r.analyte_id "
                    "WHERE r.sample_id = s.id AND a.analyte ILIKE %(analyte)s)")
        p["analyte"] = f"%{f['analyte']}%"
    return " AND ".join(cond), p


def empty_record_analytes(conn, f: dict | None = None) -> list[str]:
    """Distinct analyte names present on the empty records (the blank result rows still name an
    analyte). Shows what the artifacts are — and doubles as the filter's option list. Ignores the
    analyte filter itself so the full list is always shown."""
    scoped = {k: v for k, v in (f or {}).items() if k != "analyte"}
    where, p = _where(scoped)
    return [r["analyte"] for r in conn.execute(
        f"""SELECT a.analyte, count(*) AS n {_FROM}
            JOIN result r ON r.sample_id = s.id JOIN analyte a ON a.id = r.analyte_id
            WHERE {where} AND a.analyte IS NOT NULL
            GROUP BY a.analyte ORDER BY n DESC, a.analyte""", p).fetchall()]


def empty_lab_records(conn, f: dict | None = None, *, limit=500, offset=0) -> list:
    """Samples that look like empty ingestion artifacts, worst (least identity) grouped by batch."""
    where, p = _where(f or {})
    p["limit"], p["offset"] = limit, offset
    return conn.execute(
        f"""SELECT s.id, st.station_code, s.sample_date, s.bg_id, s.lab_sample_id, s.sample_type,
                   s.bloom_report_id, s.case_id, s.lab_batch_id AS event_id, b.source AS batch_source,
                   b.uploaded_at AS ingested_at, st.geom IS NOT NULL AS geocoded,
                   (SELECT count(*) FROM result r WHERE r.sample_id = s.id) AS n_results
            {_FROM} WHERE {where}
            ORDER BY s.lab_batch_id NULLS LAST, s.id LIMIT %(limit)s OFFSET %(offset)s""", p).fetchall()


def count_empty_records(conn, f: dict | None = None) -> int:
    where, p = _where(f or {})
    return conn.execute(f"SELECT count(*) AS c {_FROM} WHERE {where}", p).fetchone()["c"]


def delete_samples(conn, user_id, sample_ids, *, include_linked=False) -> dict:
    """Delete the given samples — but ONLY those that are genuinely empty (no substantive result)
    and, unless include_linked, not tied to a report/case. Returns {deleted, skipped}. Sets the
    audit actor so each deletion is attributable in the audit log."""
    ids = [int(x) for x in sample_ids if str(x).isdigit()]
    if not ids:
        return {"deleted": 0, "skipped": 0}
    conn.execute("SELECT set_config('fhab.user_id', %s, false)", (str(user_id or ""),))
    guard = "" if include_linked else " AND s.bloom_report_id IS NULL AND s.case_id IS NULL"
    eligible = [r["id"] for r in conn.execute(
        f"SELECT s.id FROM sample s WHERE s.id = ANY(%s) AND NOT {_HAS_DATA}{guard}", (ids,)).fetchall()]
    deleted = purge_samples(conn, eligible)
    conn.commit()
    return {"deleted": deleted, "skipped": len(ids) - len(eligible)}


def purge_samples(conn, ids: list[int]) -> int:
    """Low-level delete of samples + their child rows (results, and the FK refs that don't cascade).
    No eligibility check — callers must guard. Does not commit. Returns rows deleted."""
    if not ids:
        return 0
    conn.execute("DELETE FROM sample_link WHERE sample_id = ANY(%s)", (ids,))
    conn.execute("UPDATE lab_stage_sample SET linked_sample = NULL WHERE linked_sample = ANY(%s)", (ids,))
    conn.execute("DELETE FROM result WHERE sample_id = ANY(%s)", (ids,))
    return conn.execute("DELETE FROM sample WHERE id = ANY(%s)", (ids,)).rowcount
