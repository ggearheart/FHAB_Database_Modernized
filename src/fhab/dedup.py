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


def candidate_duplicate_samples(conn, *, limit: int = 200) -> list:
    """Groups of 2+ samples sharing an identity fingerprint. Each group carries its members."""
    return conn.execute(
        f"""
        WITH keyed AS (
            SELECT s.id, {_KEY} AS k, st.station_code, s.sample_date, s.sample_type,
                   s.bg_id, s.lab_sample_id, s.sample_id, s.lab_batch_id, b.source AS batch_source,
                   s.bloom_report_id, s.case_id, s.sampling_type,
                   (SELECT count(*) FROM result r WHERE r.sample_id = s.id) AS n_results
            FROM sample s
            LEFT JOIN station st ON st.id = s.station_id
            LEFT JOIN lab_batch b ON b.id = s.lab_batch_id
        )
        SELECT k AS key, count(*) AS n,
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
        LIMIT %s""", (limit,)).fetchall()


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
