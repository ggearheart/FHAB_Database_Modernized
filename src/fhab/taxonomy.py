"""Analyte taxonomy administration: curate the analyte vocabulary and merge aliases.

CEDEN ingest auto-creates an analyte row for every distinct name it sees, so the table accrues
aliases (e.g. "Microcystin", "Microcystins total", "mcyE"). These helpers let a program admin
fix an analyte's canonical fields and merge an alias into a canonical analyte — repointing all
results, then removing the alias — so analytics and exports read cleanly.

Operations run on the privileged connection (the screens are program-admin only). analyte has no
RLS (reference vocabulary); the only FK into it is result.analyte_id.
"""

from __future__ import annotations

import psycopg


class TaxonomyError(ValueError):
    """A taxonomy edit could not be applied (message is safe to show)."""


def list_analytes(conn: psycopg.Connection) -> list:
    """All analytes with how many results reference each (for the admin screen)."""
    return conn.execute(
        """SELECT a.id, a.analysis_type, a.analyte_class, a.analyte, a.default_unit,
                  (SELECT count(*) FROM result r WHERE r.analyte_id = a.id) AS n_results
           FROM analyte a
           ORDER BY a.analysis_type NULLS LAST, a.analyte_class NULLS LAST,
                    lower(a.analyte) NULLS LAST""").fetchall()


def update_analyte(conn: psycopg.Connection, aid: int, *, analysis_type=None, analyte_class=None,
                   analyte=None, default_unit=None) -> None:
    """Edit an analyte's canonical fields. Raises TaxonomyError if it would collide with another."""
    if not (analyte or "").strip():
        raise TaxonomyError("analyte name is required")
    try:
        conn.execute(
            """UPDATE analyte SET analysis_type=%s, analyte_class=%s, analyte=%s, default_unit=%s
               WHERE id=%s""",
            (analysis_type or None, analyte_class or None, analyte.strip(),
             default_unit or None, aid))
        conn.commit()
    except psycopg.errors.UniqueViolation:
        conn.rollback()
        raise TaxonomyError("another analyte already has that analysis type + class + name — "
                            "merge into it instead")


def merge_analytes(conn: psycopg.Connection, source_id: int, target_id: int) -> int:
    """Repoint every result from `source_id` to `target_id`, then delete the source. Returns moved."""
    if source_id == target_id:
        raise TaxonomyError("pick a different analyte to merge into")
    if not conn.execute("SELECT 1 FROM analyte WHERE id=%s", (target_id,)).fetchone():
        raise TaxonomyError("target analyte not found")
    if not conn.execute("SELECT 1 FROM analyte WHERE id=%s", (source_id,)).fetchone():
        raise TaxonomyError("source analyte not found")
    moved = conn.execute("UPDATE result SET analyte_id=%s WHERE analyte_id=%s",
                         (target_id, source_id)).rowcount
    conn.execute("DELETE FROM analyte WHERE id=%s", (source_id,))
    conn.commit()
    return moved


def delete_analyte(conn: psycopg.Connection, aid: int) -> None:
    """Delete an analyte that no results reference. Raises TaxonomyError if it is in use."""
    n = conn.execute("SELECT count(*) AS c FROM result WHERE analyte_id=%s", (aid,)).fetchone()["c"]
    if n:
        raise TaxonomyError(f"in use by {n} result(s) — merge it into a canonical analyte instead")
    conn.execute("DELETE FROM analyte WHERE id=%s", (aid,))
    conn.commit()
