"""Governance #1: detect and merge duplicate lab samples across ingest paths.

Multiple ingest paths (folder, CEDEN, manual, data.ca.gov refresh) feed `sample`/`result`, and
only `bg_id` is uniqueness-constrained. Samples for the *same* physical collection can therefore
land more than once under different keys. This module surfaces **candidate** duplicate groups by a
normalized identity fingerprint and lets a staffer merge a group into one survivor — repointing all
child rows and de-duplicating the survivor's results.

Detection is candidate-based (station+date+type can also be legitimate field replicates), so a
human confirms every merge. Fully-automatic folder↔data.ca.gov matching is limited by the absence
of a shared identifier across those sources (see docs/GOVERNANCE_REVIEW.md #1).
"""

from __future__ import annotations

# Normalized identity fingerprint for a sample: prefer an explicit lab id, else the
# station + date + type "collection" key. concat_ws skips NULLs.
_KEY = """lower(trim(coalesce(
    nullif(s.lab_sample_id, ''), nullif(s.sample_id, ''), nullif(s.bg_id, ''),
    nullif(concat_ws('|', st.station_code, s.sample_date::text, s.sample_type), ''))))"""


# What the fingerprint was based on — surfaced so a reviewer sees *why* two rows matched.
_KEY_KIND = """CASE
    WHEN nullif(s.lab_sample_id, '') IS NOT NULL THEN 'lab sample id: ' || s.lab_sample_id
    WHEN nullif(s.sample_id, '') IS NOT NULL THEN 'sample id: ' || s.sample_id
    WHEN nullif(s.bg_id, '') IS NOT NULL THEN 'BG_ID: ' || s.bg_id
    ELSE 'station + date + type' END"""


def candidate_duplicate_samples(conn, *, q=None, batch=None, limit: int = 200) -> list:
    """Groups of 2+ samples sharing an identity fingerprint. Each group carries its members and
    the reason they matched. Optional filter by station/BG_ID/source (q) or sampling event (batch)."""
    cond, p = [], {"limit": limit}
    if q:
        cond.append("(st.station_code ILIKE %(q)s OR s.bg_id ILIKE %(q)s OR s.lab_sample_id ILIKE %(q)s "
                    "OR b.source ILIKE %(q)s)")
        p["q"] = f"%{q}%"
    if str(batch or "").isdigit():
        cond.append("s.lab_batch_id = %(batch)s"); p["batch"] = int(batch)
    where = (" WHERE " + " AND ".join(cond)) if cond else ""
    return conn.execute(
        f"""
        WITH keyed AS (
            SELECT s.id, {_KEY} AS k, {_KEY_KIND} AS match_on, st.station_code, s.sample_date,
                   s.sample_type, s.bg_id, s.lab_sample_id, s.sample_id, s.lab_batch_id,
                   b.source AS batch_source, s.bloom_report_id, s.case_id, s.sampling_type,
                   (SELECT count(*) FROM result r WHERE r.sample_id = s.id) AS n_results
            FROM sample s
            LEFT JOIN station st ON st.id = s.station_id
            LEFT JOIN lab_batch b ON b.id = s.lab_batch_id{where}
        )
        SELECT k AS key, count(*) AS n, min(match_on) AS match_on,
               json_agg(json_build_object(
                   'id', id, 'station_code', station_code, 'sample_date', sample_date,
                   'sample_type', sample_type, 'bg_id', bg_id, 'lab_sample_id', lab_sample_id,
                   'sample_id', sample_id, 'lab_batch_id', lab_batch_id, 'batch_source', batch_source,
                   'n_results', n_results, 'bloom_report_id', bloom_report_id, 'case_id', case_id,
                   'sampling_type', sampling_type) ORDER BY n_results DESC, id) AS members
        FROM keyed
        WHERE k IS NOT NULL
        GROUP BY k HAVING count(*) > 1
        ORDER BY count(*) DESC, k
        LIMIT %(limit)s""", p).fetchall()


# Possible duplicate reports/events: same water body, observation date, and region.
_EKEY = "lower(trim(concat_ws('|', w.water_body_name, e.observation_date::text, w.regional_water_board)))"
_EFROM = ("FROM event e LEFT JOIN location l ON l.id = e.location_id "
          "LEFT JOIN waterbody w ON w.id = l.waterbody_id")


def candidate_duplicate_events(conn, *, limit: int = 100) -> list:
    """Groups of 2+ reports for the same water body, date and region — possible duplicate events.
    Read-only (report de-duplication is a manual review; cases/responses hang off these ids)."""
    return conn.execute(
        f"""
        WITH ek AS (
            SELECT e.bloom_report_id, e.observation_date, e.case_id, e.determination_code,
                   w.water_body_name, w.regional_water_board, w.county, {_EKEY} AS k,
                   (SELECT count(*) FROM sample s WHERE s.bloom_report_id = e.bloom_report_id) AS n_samples
            {_EFROM})
        SELECT k AS key, count(*) AS n,
               json_agg(json_build_object(
                   'bloom_report_id', bloom_report_id, 'observation_date', observation_date,
                   'water_body_name', water_body_name, 'regional_water_board', regional_water_board,
                   'county', county, 'case_id', case_id, 'determination_code', determination_code,
                   'n_samples', n_samples) ORDER BY bloom_report_id) AS members
        FROM ek
        WHERE water_body_name IS NOT NULL AND observation_date IS NOT NULL
        GROUP BY k HAVING count(*) > 1
        ORDER BY count(*) DESC, k LIMIT %s""", (limit,)).fetchall()


def duplicate_summary(conn) -> dict:
    """Headline counts for the report: candidate duplicate groups and the redundant records
    (rows beyond the first in each group) for samples and events."""
    s = conn.execute(
        f"""SELECT count(*) AS groups, coalesce(sum(n - 1), 0) AS extras FROM (
                SELECT {_KEY} AS k, count(*) AS n
                FROM sample s LEFT JOIN station st ON st.id = s.station_id
                GROUP BY 1 HAVING count(*) > 1) g WHERE k IS NOT NULL""").fetchone()
    e = conn.execute(
        f"""SELECT count(*) AS groups, coalesce(sum(n - 1), 0) AS extras FROM (
                SELECT {_EKEY} AS k, count(*) AS n {_EFROM}
                WHERE w.water_body_name IS NOT NULL AND e.observation_date IS NOT NULL
                GROUP BY 1 HAVING count(*) > 1) g""").fetchone()
    return {"sample_groups": s["groups"], "sample_extras": s["extras"],
            "event_groups": e["groups"], "event_extras": e["extras"]}


def delete_samples(conn, user_id, sample_ids, *, allow_linked=False) -> dict:
    """Batch-delete selected duplicate samples (and their results). Skips samples linked to a
    report/case unless allow_linked — merge those instead. Audited via the sample delete trigger."""
    from .cleanup import purge_samples
    ids = [int(x) for x in sample_ids if str(x).isdigit()]
    if not ids:
        return {"deleted": 0, "skipped": 0}
    conn.execute("SELECT set_config('fhab.user_id', %s, false)", (str(user_id or ""),))
    guard = "" if allow_linked else " AND bloom_report_id IS NULL AND case_id IS NULL"
    eligible = [r["id"] for r in conn.execute(
        f"SELECT id FROM sample WHERE id = ANY(%s){guard}", (ids,)).fetchall()]
    deleted = purge_samples(conn, eligible)
    conn.commit()
    return {"deleted": deleted, "skipped": len(ids) - len(eligible)}


def duplicate_count(conn) -> int:
    """Number of candidate duplicate groups (for the hub badge)."""
    return conn.execute(
        f"""SELECT count(*) AS c FROM (
                SELECT {_KEY} AS k
                FROM sample s LEFT JOIN station st ON st.id = s.station_id
                GROUP BY 1 HAVING count(*) > 1) g
            WHERE k IS NOT NULL""").fetchone()["c"]


def merge_samples(conn, user_id, survivor_id: int, member_ids) -> dict:
    """Merge duplicate samples into `survivor_id`. Runs on the owner connection (repoints tables
    fhab_app can't write). Repoints child rows, de-dups the survivor's results, deletes the dups."""
    survivor_id = int(survivor_id)
    dups = [int(i) for i in member_ids if int(i) != survivor_id]
    if not dups:
        return {"merged": 0, "results_repointed": 0, "results_deduped": 0}
    if not conn.execute("SELECT 1 FROM sample WHERE id=%s", (survivor_id,)).fetchone():
        raise ValueError("Survivor sample not found.")
    try:
        moved = conn.execute("UPDATE result SET sample_id=%s WHERE sample_id = ANY(%s)",
                             (survivor_id, dups)).rowcount
        conn.execute("UPDATE sample_link SET sample_id=%s WHERE sample_id = ANY(%s)", (survivor_id, dups))
        conn.execute("UPDATE lab_stage_sample SET linked_sample=%s WHERE linked_sample = ANY(%s)",
                     (survivor_id, dups))
        # sample_station_link has UNIQUE(sample_id, station_code): drop dup links the survivor
        # already has, then repoint the rest.
        conn.execute(
            """DELETE FROM sample_station_link d WHERE d.sample_id = ANY(%s)
               AND EXISTS (SELECT 1 FROM sample_station_link k
                           WHERE k.sample_id=%s AND k.station_code=d.station_code)""", (dups, survivor_id))
        conn.execute("UPDATE sample_station_link SET sample_id=%s WHERE sample_id = ANY(%s)",
                     (survivor_id, dups))
        # De-dup the survivor's results: one per (analyte, method, fraction), keep lowest key.
        deduped = conn.execute(
            """DELETE FROM result r USING (
                   SELECT result_id_unique, row_number() OVER (
                       PARTITION BY coalesce(analyte_id,-1), coalesce(method,''), coalesce(fraction_name,'')
                       ORDER BY result_id_unique) AS rn
                   FROM result WHERE sample_id=%s) d
               WHERE r.result_id_unique = d.result_id_unique AND d.rn > 1""", (survivor_id,)).rowcount
        merged = conn.execute("DELETE FROM sample WHERE id = ANY(%s)", (dups,)).rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"merged": merged, "results_repointed": moved, "results_deduped": deduped}
