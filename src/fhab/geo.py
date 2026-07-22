"""Geospatial backbone: load authoritative boundary layers (HUC-12 watersheds, CA counties,
Regional Water Board boundaries), derive station/location attributes by point-in-polygon, and
mint Geoconnex persistent identifiers. See docs/GEOCONNEX.md (GEO-1, GEO-4).

The boundary layers come from authoritative services and are the source of truth for the
crosswalk export's HUC12 / County / Regional_Water_Board (and the watershed-scale Water_Body_Name
fallback). `fetch_layer` pulls a layer as GeoJSON from its ArcGIS REST endpoint; the loaders below
insert those FeatureCollections; `derive_geo` assigns them to points.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import psycopg

GEOCONNEX = "https://geoconnex.us/ca-fhab"

# Authoritative source layers (ArcGIS REST). Each: (query_url, out_fields, where). All support
# f=geojson + resultOffset paging and return WGS84 when asked (outSR=4326).
SOURCES = {
    # USGS Watershed Boundary Dataset, 12-digit HU (subwatershed) layer, CA subset.
    "huc12": ("https://hydro.nationalmap.gov/arcgis/rest/services/wbd/MapServer/6/query",
              "huc12,name,hutype,tohuc,areasqkm", "states LIKE '%CA%'"),
    # CA State Geoportal authoritative county boundaries.
    "county": ("https://services.gis.ca.gov/arcgis/rest/services/Boundaries/CA_Counties/FeatureServer/0/query",
               "County,FIPS", "1=1"),
    # CA Water Boards Regional Board Boundaries (RB 1-9), hosted (portalserver) copy.
    "regional_board": ("https://gispublic.waterboards.ca.gov/portalserver/rest/services/Hosted/Regional_Board_Boundary_Features/FeatureServer/1/query",
                       "rb,rb_name", "1=1"),
}

# A browser User-Agent — the CA Water Boards WAF serves a bot challenge (HTML) to the default
# curl/urllib agents; a normal browser UA is served the data.
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


# Server-side geometry generalization: the raw WBD polygons are far too detailed to fetch whole
# (~8 MB per 250 features). ~55 m simplification keeps point-in-polygon assignment correct at
# watershed/county/region scale while shrinking the payload ~10x. Page 500 keeps each request fast.
_GEOM_PRECISION = 5
_MAX_OFFSET = 0.0005
_PAGE = 500


def fetch_layer(name: str, page: int = _PAGE) -> dict:
    """Download an authoritative boundary layer as a GeoJSON FeatureCollection (paged via curl).

    curl is used (not urllib) because some agency WAFs block Python's user agent; it is already a
    runtime dependency (see fhab.refresh) and present on Render.
    """
    if not shutil.which("curl"):
        raise RuntimeError("curl is required to fetch boundary layers but was not found on PATH.")
    from urllib.parse import urlencode
    url, out_fields, where = SOURCES[name]
    features: list[dict] = []
    offset = 0
    while True:
        params = {"where": where, "outFields": out_fields, "returnGeometry": "true",
                  "outSR": "4326", "geometryPrecision": _GEOM_PRECISION,
                  "maxAllowableOffset": _MAX_OFFSET, "orderByFields": out_fields.split(",")[0],
                  "resultOffset": offset, "resultRecordCount": page, "f": "geojson"}
        raw = subprocess.run(["curl", "-fsSL", "--max-time", "150", "-A", _UA,
                              f"{url}?{urlencode(params)}"],
                             check=True, capture_output=True, text=True, timeout=180).stdout
        feats = json.loads(raw).get("features", [])
        features.extend(feats)
        if len(feats) < page:
            break
        offset += page
    return {"type": "FeatureCollection", "features": features}


def _props_ci(feature: dict) -> dict:
    """Case-insensitive property access (ArcGIS field casing varies by service)."""
    return {(k or "").lower(): v for k, v in (feature.get("properties") or {}).items()}


def _feature_collection(fc_or_path) -> dict:
    if isinstance(fc_or_path, (str, Path)):
        return json.loads(Path(fc_or_path).read_text())
    return fc_or_path


def load_huc12(conn: psycopg.Connection, fc_or_path) -> int:
    """Load a HUC-12 GeoJSON FeatureCollection (or file path) into the huc12 table. Returns rows."""
    fc = _feature_collection(fc_or_path)
    n = 0
    with conn.cursor() as cur:
        for feat in fc.get("features", []):
            p = _props_ci(feat)
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


def load_counties(conn: psycopg.Connection, fc_or_path) -> int:
    """Load CA county boundaries (fields County, FIPS) into ca_county. Returns rows loaded."""
    fc = _feature_collection(fc_or_path)
    n = 0
    with conn.cursor() as cur:
        for feat in fc.get("features", []):
            p = _props_ci(feat)
            name = (p.get("county") or "").strip()
            geom = feat.get("geometry")
            if not name or geom is None:
                continue
            cur.execute(
                """INSERT INTO ca_county (county, fips, geom)
                   VALUES (%s,%s, ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(%s),4326)))
                   ON CONFLICT (county) DO UPDATE SET fips = EXCLUDED.fips, geom = EXCLUDED.geom""",
                (name, p.get("fips"), json.dumps(geom)),
            )
            n += 1
    conn.commit()
    return n


def load_regional_boards(conn: psycopg.Connection, fc_or_path) -> int:
    """Load Regional Water Board boundaries (fields RB, RB_NAME) into regional_board.

    regional_water_board is formatted "Region {RB} - {RB_NAME}" to match the app's usage.
    """
    fc = _feature_collection(fc_or_path)
    n = 0
    with conn.cursor() as cur:
        for feat in fc.get("features", []):
            p = _props_ci(feat)
            rb, rb_name = p.get("rb"), (p.get("rb_name") or "").strip()
            geom = feat.get("geometry")
            if rb is None or geom is None:
                continue
            rwb = f"Region {int(rb)} - {rb_name}" if rb_name else f"Region {int(rb)}"
            cur.execute(
                """INSERT INTO regional_board (rb, rb_name, regional_water_board, geom)
                   VALUES (%s,%s,%s, ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(%s),4326)))
                   ON CONFLICT (rb) DO UPDATE SET rb_name = EXCLUDED.rb_name,
                     regional_water_board = EXCLUDED.regional_water_board, geom = EXCLUDED.geom""",
                (int(rb), rb_name or None, rwb, json.dumps(geom)),
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


def derive_county(conn: psycopg.Connection) -> int:
    """Assign station.county by point-in-polygon against ca_county. Returns rows updated."""
    rows = conn.execute(
        """UPDATE station s SET county = c.county
           FROM ca_county c
           WHERE s.geom IS NOT NULL AND s.county IS DISTINCT FROM c.county
             AND ST_Intersects(c.geom, s.geom)
           RETURNING s.id""").fetchall()
    conn.commit()
    return len(rows)


def derive_region(conn: psycopg.Connection) -> int:
    """Assign station.regional_water_board by point-in-polygon against regional_board. Returns rows."""
    rows = conn.execute(
        """UPDATE station s SET regional_water_board = b.regional_water_board
           FROM regional_board b
           WHERE s.geom IS NOT NULL AND s.regional_water_board IS DISTINCT FROM b.regional_water_board
             AND ST_Intersects(b.geom, s.geom)
           RETURNING s.id""").fetchall()
    conn.commit()
    return len(rows)


def derive_geo(conn: psycopg.Connection) -> dict:
    """Assign HUC12 (station+location), county and region to every geocoded station."""
    return {"huc12": derive_huc12(conn),
            "county": derive_county(conn),
            "region": derive_region(conn)}


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
    """Load HUC-12, derive watersheds, and mint PIDs in one pass (file-based; used by scripts)."""
    report = {"huc12_loaded": load_huc12(conn, huc12_geojson)}
    report["derived"] = derive_huc12(conn)
    report["minted"] = mint_geoconnex(conn)
    return report


def refresh_boundaries(conn: psycopg.Connection) -> dict:
    """Fetch the authoritative boundary layers from their services, load them, and re-derive.

    One admin action: HUC12 + counties + regional boards -> point-in-polygon onto every station.
    Returns loaded/derived counts for display.
    """
    loaded = {
        "huc12": load_huc12(conn, fetch_layer("huc12")),
        "county": load_counties(conn, fetch_layer("county")),
        "regional_board": load_regional_boards(conn, fetch_layer("regional_board")),
    }
    return {"loaded": loaded, "derived": derive_geo(conn), "minted": mint_geoconnex(conn)}


def boundary_status(conn: psycopg.Connection) -> dict:
    """Row counts for the boundary tables + how many stations are enriched (for the admin page)."""
    one = lambda q: conn.execute(q).fetchone()["c"]
    return {
        "huc12": one("SELECT count(*) c FROM huc12"),
        "county": one("SELECT count(*) c FROM ca_county"),
        "regional_board": one("SELECT count(*) c FROM regional_board"),
        "stations_total": one("SELECT count(*) c FROM station WHERE geom IS NOT NULL"),
        "stations_huc12": one("SELECT count(*) c FROM station WHERE huc12 IS NOT NULL"),
        "stations_county": one("SELECT count(*) c FROM station WHERE county IS NOT NULL"),
        "stations_region": one("SELECT count(*) c FROM station WHERE regional_water_board IS NOT NULL"),
    }
