"""One-time backfill of event.determination_code from existing advisory signals.

Derives the likely outcome of historical reports from their advisory recommendations and
advisory-detail text. Only fills events whose determination is not already set, so it never
overrides a value a staffer recorded. Heuristic — most-specific finding wins.
"""

from __future__ import annotations

import psycopg

# Ordered: the first matching rule wins (specific non-HAB / marine / spill findings take
# precedence over the generic "an advisory was posted -> confirmed HAB" inference).
_BACKFILL_SQL = """
WITH sig AS (
    SELECT e.bloom_report_id,
           lower(string_agg(
               coalesce(a.advisory_detail, '') || ' ' || coalesce(a.advisory_recommended, ''), ' ')
           ) AS txt
    FROM event e
    JOIN response r ON r.bloom_report_id = e.bloom_report_id
    JOIN advisory a ON a.response_action_id = r.response_action_id
    GROUP BY e.bloom_report_id
)
UPDATE event e
SET determination_code = CASE
    WHEN sig.txt LIKE '%marine%' OR sig.txt LIKE '%red tide%'            THEN 'red_tide'
    WHEN sig.txt LIKE '%spill%'                                          THEN 'spill'
    WHEN sig.txt LIKE '%no cyano%' OR sig.txt LIKE '%other algae%'       THEN 'non_hab_algae'
    WHEN sig.txt LIKE '%confirmed no bloom%' OR sig.txt LIKE '%de-watered%'
         OR sig.txt LIKE '%dry site%'                                    THEN 'no_bloom'
    WHEN sig.txt LIKE '%caution%' OR sig.txt LIKE '%warning%' OR sig.txt LIKE '%danger%'
         OR sig.txt LIKE '%algal mat alert%' OR sig.txt LIKE '%toxins%'
         OR sig.txt LIKE '%bloom observed%' OR sig.txt LIKE '%alert-%'
         OR sig.txt LIKE '%visual observation%'                          THEN 'confirmed_hab'
    ELSE e.determination_code
END
FROM sig
WHERE sig.bloom_report_id = e.bloom_report_id
  AND e.determination_code IS NULL
RETURNING e.determination_code AS code
"""


def backfill_determination(conn: psycopg.Connection) -> dict[str, int]:
    """Fill determination for historical reports from advisory signals. Returns counts by code."""
    rows = conn.execute(_BACKFILL_SQL).fetchall()
    conn.commit()
    counts: dict[str, int] = {}
    for r in rows:
        if r["code"] is not None:   # null = had advisories but no matching signal
            counts[r["code"]] = counts.get(r["code"], 0) + 1
    return counts
