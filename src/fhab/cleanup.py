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


def _where(f: dict) -> tuple[str, dict]:
    cond = [f"NOT {_HAS_DATA}"]
    p: dict = {}
    if not f.get("include_linked"):
        cond.append("s.bloom_report_id IS NULL AND s.case_id IS NULL")
    if str(f.get("batch") or "").isdigit():
        cond.append("s.lab_batch_id = %(batch)s"); p["batch"] = int(f["batch"])
    if f.get("q"):
        cond.append("(st.station_code ILIKE %(q)s OR s.bg_id ILIKE %(q)s OR b.source ILIKE %(q)s)")
        p["q"] = f"%{f['q']}%"
    return " AND ".join(cond), p


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
    deleted = 0
    if eligible:
        # clear the FK references that don't cascade, then the results, then the sample
        conn.execute("DELETE FROM sample_link WHERE sample_id = ANY(%s)", (eligible,))
        conn.execute("UPDATE lab_stage_sample SET linked_sample = NULL WHERE linked_sample = ANY(%s)",
                     (eligible,))
        conn.execute("DELETE FROM result WHERE sample_id = ANY(%s)", (eligible,))
        deleted = conn.execute("DELETE FROM sample WHERE id = ANY(%s)", (eligible,)).rowcount
    conn.commit()
    return {"deleted": deleted, "skipped": len(ids) - len(eligible)}
