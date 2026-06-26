"""Geospatial backbone: load HUC-12 watersheds, derive them by point-in-polygon,
and mint Geoconnex persistent identifiers. See docs/GEOCONNEX.md (GEO-1, GEO-4).
"""

from __future__ import annotations

import json
from pathlib import Path

import psycopg

GEOCONNEX = "https://geoconnex.us/ca-fhab"


def load_huc12(conn: psycopg.Connection, geojson_path: Path) -> int:
    """Load a HUC-12 GeoJSON FeatureCollection into the huc12 table. Returns rows loaded."""
    fc = json.loads(Path(geojson_path).read_text())
    n = 0
    with conn.cursor() as cur:
        for feat in fc.get("features", []):
            p = feat.get("properties", {})
            code = (p.get("huc12") or "").strip()
            geom = feat.get("geometry")
            if not code or geom is None:
                continue
            cur.execute(
                """INSERT INTO huc12 (huc12, name, hutype, tohuc, areasqkm, geom)
                   VALUES (%s,%s,%s,%s,%s, ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(%s),4326)))
                   ON CONFLICT (huc12) DO UPDATE SET
                     name = EXCLUDED.name, geom = EXCLUDED.geom""",
                (code, p.get("name"), p.get("hutype"), p.get("tohuc"),
                 p.get("areasqkm"), json.dumps(geom)),
            )
            n += 1
    conn.commit()
    return n


def derive_huc12(conn: psycopg.Connection) -> dict[str, int]:
    """Assign location.huc12 and station.huc12 by point-in-polygon. Returns counts."""
    out = {}
    for table in ("location", "station"):
        rows = conn.execute(
            f"""
            UPDATE {table} t
            SET huc12 = h.huc12
            FROM huc12 h
            WHERE t.geom IS NOT NULL
              AND t.huc12 IS DISTINCT FROM h.huc12
              AND ST_Contains(h.geom, t.geom)
            RETURNING t.huc12
            """
        ).fetchall()
        out[table] = len(rows)
    conn.commit()
    return out


def mint_geoconnex(conn: psycopg.Connection) -> dict[str, int]:
    """Mint Geoconnex PIDs for events and stations where missing. Returns counts."""
    events = conn.execute(
        f"""UPDATE event SET geoconnex_uri = '{GEOCONNEX}/events/' || bloom_report_id
            WHERE geoconnex_uri IS NULL RETURNING bloom_report_id"""
    ).fetchall()
    stations = conn.execute(
        f"""UPDATE station SET geoconnex_uri = '{GEOCONNEX}/sites/' || id
            WHERE geoconnex_uri IS NULL AND station_code IS NOT NULL RETURNING id"""
    ).fetchall()
    conn.commit()
    return {"events": len(events), "stations": len(stations)}


def build_geospatial_backbone(conn: psycopg.Connection, huc12_geojson: Path) -> dict:
    """Load HUC-12, derive watersheds, and mint PIDs in one pass."""
    report = {"huc12_loaded": load_huc12(conn, huc12_geojson)}
    report["derived"] = derive_huc12(conn)
    report["minted"] = mint_geoconnex(conn)
    return report
