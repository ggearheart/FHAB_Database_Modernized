"""Tests for the geospatial backbone: HUC-12 load, point-in-polygon, Geoconnex PIDs."""

from pathlib import Path

from fhab.geo import derive_huc12, load_huc12, mint_geoconnex

HUC12_FIXTURE = Path(__file__).parent / "fixtures" / "geo" / "huc12_sample.geojson"

# A point inside the test watershed polygon (-123..-122.5 lon, 37.8..38.2 lat).
INSIDE = "ST_SetSRID(ST_MakePoint(-122.8675, 38.0525), 4326)"


def test_load_huc12_with_generated_ref_uri(conn):
    n = load_huc12(conn, HUC12_FIXTURE)
    assert n == 1
    row = conn.execute(
        "SELECT huc12, name, geoconnex_uri, ST_GeometryType(geom) AS gtype FROM huc12"
    ).fetchone()
    assert row["huc12"].strip() == "180500059999"
    assert row["geoconnex_uri"] == "https://geoconnex.us/ref/hu12/180500059999"
    assert row["gtype"] == "ST_MultiPolygon"


def test_derive_huc12_by_point_in_polygon(conn):
    load_huc12(conn, HUC12_FIXTURE)
    wb = conn.execute(
        "INSERT INTO waterbody (water_body_name) VALUES ('X') RETURNING id"
    ).fetchone()["id"]
    conn.execute(f"INSERT INTO location (waterbody_id, geom) VALUES (%s, {INSIDE})", (wb,))
    conn.execute(
        f"INSERT INTO station (station_code, station_name, geom) VALUES ('S1', 'S', {INSIDE})"
    )
    conn.commit()

    counts = derive_huc12(conn)
    assert counts["location"] == 1
    assert counts["station"] == 1
    assert conn.execute(
        "SELECT huc12 FROM location WHERE huc12 IS NOT NULL"
    ).fetchone()["huc12"].strip() == "180500059999"


def test_mint_geoconnex_pids(conn):
    wb = conn.execute(
        "INSERT INTO waterbody (water_body_name) VALUES ('X') RETURNING id"
    ).fetchone()["id"]
    loc = conn.execute(
        "INSERT INTO location (waterbody_id) VALUES (%s) RETURNING id", (wb,)
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO event (bloom_report_id, location_id) VALUES (12345, %s)", (loc,)
    )
    conn.execute("INSERT INTO station (station_code, station_name) VALUES ('S1', 'S')")
    conn.commit()

    minted = mint_geoconnex(conn)
    assert minted["events"] == 1
    assert minted["stations"] == 1
    assert conn.execute(
        "SELECT geoconnex_uri FROM event WHERE bloom_report_id = 12345"
    ).fetchone()["geoconnex_uri"] == "https://geoconnex.us/ca-fhab/events/12345"
    assert conn.execute(
        "SELECT geoconnex_uri FROM station WHERE station_code = 'S1'"
    ).fetchone()["geoconnex_uri"].startswith("https://geoconnex.us/ca-fhab/sites/")


def test_mint_is_idempotent(conn):
    conn.execute("INSERT INTO station (station_code, station_name) VALUES ('S1', 'S')")
    conn.commit()
    assert mint_geoconnex(conn)["stations"] == 1
    assert mint_geoconnex(conn)["stations"] == 0  # already minted; not re-minted
