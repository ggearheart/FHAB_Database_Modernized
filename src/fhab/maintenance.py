"""Maintenance / reset operations for the test environment.

Shared by the CLI (scripts/purge_lab_data.py) and the admin Reset screen so both delete exactly
the same set, in the same foreign-key order.
"""

from __future__ import annotations

import psycopg

# Lab-data tables, children first so foreign keys are satisfied on delete.
LAB_TABLES = ["result", "sample_link", "lab_stage_result", "lab_stage_sample", "lab_batch", "sample"]
# Shown for reassurance; never touched by purge_lab_data.
KEPT_TABLES = ["event", "hab_case", "analyte", "station", "public_report_submission"]


def lab_data_counts(conn: psycopg.Connection) -> dict:
    """Row counts for every lab-data table (to clear) and the preserved tables."""
    return {t: conn.execute(f"SELECT count(*) AS c FROM {t}").fetchone()["c"]
            for t in LAB_TABLES + KEPT_TABLES}


def purge_lab_data(conn: psycopg.Connection) -> dict:
    """Delete all lab data (samples, results, sample-link rows, lab-batch staging).

    Keeps reports/events/cases, the public submission queue, the analyte vocabulary, and the
    station registry/stations. Runs in one transaction; rolls back on any error. Returns the
    number of rows deleted per table.
    """
    deleted = {}
    try:
        for t in LAB_TABLES:
            deleted[t] = conn.execute(f"DELETE FROM {t}").rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return deleted
