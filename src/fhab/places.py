"""Controlled-vocabulary helpers for places.

County is a closed list (the official bloom-report form's options). Waterbody is an open set,
so instead of a dropdown we fuzzy-match (pg_trgm) against the waterbodies already in the system
to steer staff toward the canonical spelling and away from near-duplicates.
"""

from __future__ import annotations

import psycopg

# Counties exactly as the MyWaterQuality "Report a HAB" form lists them (58 CA + out-of-state).
COUNTIES = [
    "Alameda", "Alpine", "Amador", "Butte", "Calaveras", "Colusa", "Contra Costa", "Del Norte",
    "El Dorado", "Fresno", "Glenn", "Humboldt", "Imperial", "Inyo", "Kern", "Kings", "Lake",
    "Lassen", "Los Angeles", "Madera", "Marin", "Mariposa", "Mendocino", "Merced", "Modoc",
    "Mono", "Monterey", "Napa", "Nevada", "Orange", "Placer", "Plumas", "Riverside", "Sacramento",
    "San Benito", "San Bernardino", "San Diego", "San Francisco", "San Joaquin", "San Luis Obispo",
    "San Mateo", "Santa Barbara", "Santa Clara", "Santa Cruz", "Shasta", "Sierra", "Siskiyou",
    "Solano", "Sonoma", "Stanislaus", "Sutter", "Tehama", "Trinity", "Tulare", "Tuolumne",
    "Ventura", "Yolo", "Yuba", "Arizona State", "Nevada State", "Oregon State",
]


def suggest_waterbodies(conn: psycopg.Connection, q: str, limit: int = 8) -> list[dict]:
    """Type-ahead: existing waterbodies matching `q`, prefix matches first then fuzzy (pg_trgm)."""
    q = (q or "").strip()
    if len(q) < 2:
        return []
    rows = conn.execute(
        """SELECT id, water_body_name, county, regional_water_board,
                  similarity(water_body_name, %(q)s) AS sim
           FROM waterbody
           WHERE water_body_name %% %(q)s OR water_body_name ILIKE %(pre)s
           ORDER BY (water_body_name ILIKE %(pre)s) DESC, sim DESC, water_body_name
           LIMIT %(lim)s""",
        {"q": q, "pre": q + "%", "lim": limit}).fetchall()
    return [{"id": r["id"], "name": r["water_body_name"], "county": r["county"],
             "region": r["regional_water_board"], "sim": round(r["sim"] or 0, 2)} for r in rows]


def similar_waterbodies(conn: psycopg.Connection, name: str, county: str | None = None,
                        threshold: float = 0.5, limit: int = 5) -> list[dict]:
    """Near-duplicate guard: strongly-similar existing waterbodies, excluding an exact name match."""
    name = (name or "").strip()
    if not name:
        return []
    rows = conn.execute(
        """SELECT id, water_body_name, county, regional_water_board,
                  similarity(water_body_name, %(n)s) AS sim
           FROM waterbody
           WHERE similarity(water_body_name, %(n)s) >= %(t)s
             AND lower(water_body_name) <> lower(%(n)s)
           ORDER BY sim DESC, water_body_name
           LIMIT %(lim)s""",
        {"n": name, "t": threshold, "lim": limit}).fetchall()
    return [{"id": r["id"], "name": r["water_body_name"], "county": r["county"],
             "region": r["regional_water_board"], "sim": round(r["sim"], 2)} for r in rows]
