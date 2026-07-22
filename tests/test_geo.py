"""Tests for the geospatial backbone: HUC-12 load, point-in-polygon, Geoconnex PIDs."""

from pathlib import Path

from fhab.geo import (derive_county, derive_geo, derive_huc12, derive_region, load_counties,
                      load_huc12, load_regional_boards, mint_geoconnex, refresh_boundaries)

FIX = Path(__file__).parent / "fixtures" / "geo"
HUC12_FIXTURE = FIX / "huc12_sample.geojson"
COUNTY_FIXTURE = FIX / "county_sample.geojson"
REGION_FIXTURE = FIX / "regional_board_sample.geojson"

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


def test_load_and_derive_county_region(conn):
    assert load_counties(conn, COUNTY_FIXTURE) == 1
    r = conn.execute("SELECT county, fips, ST_GeometryType(geom) g FROM ca_county").fetchone()
    assert r["county"] == "Test County" and r["fips"] == "06999" and r["g"] == "ST_MultiPolygon"

    assert load_regional_boards(conn, REGION_FIXTURE) == 1
    b = conn.execute("SELECT rb, regional_water_board FROM regional_board").fetchone()
    assert b["rb"] == 5 and b["regional_water_board"] == "Region 5 - Central Valley"

    conn.execute(f"INSERT INTO station (station_code, geom) VALUES ('S1', {INSIDE})")
    conn.commit()
    assert derive_county(conn) == 1
    assert derive_region(conn) == 1
    s = conn.execute("SELECT county, regional_water_board FROM station WHERE station_code='S1'").fetchone()
    assert s["county"] == "Test County" and s["regional_water_board"] == "Region 5 - Central Valley"


def test_derive_geo_all_layers(conn):
    load_huc12(conn, HUC12_FIXTURE)
    load_counties(conn, COUNTY_FIXTURE)
    load_regional_boards(conn, REGION_FIXTURE)
    conn.execute(f"INSERT INTO station (station_code, geom) VALUES ('S1', {INSIDE})")
    conn.commit()
    out = derive_geo(conn)
    assert out["huc12"]["station"] == 1 and out["county"] == 1 and out["region"] == 1
    s = conn.execute("SELECT huc12, county, regional_water_board FROM station").fetchone()
    assert s["huc12"].strip() == "180500059999" and s["county"] == "Test County"


def test_refresh_boundaries_skips_already_loaded(conn):
    """Resumability: with every layer already populated, refresh_boundaries fetches nothing
    (would need the network) and only re-derives. If it tried to fetch, this test would error."""
    load_huc12(conn, HUC12_FIXTURE)
    load_counties(conn, COUNTY_FIXTURE)
    load_regional_boards(conn, REGION_FIXTURE)
    conn.execute(f"INSERT INTO station (station_code, geom) VALUES ('S1', {INSIDE})")
    conn.commit()

    rep = refresh_boundaries(conn)          # no force -> all layers kept, no fetch_layer call
    assert rep["loaded"] == {}
    assert set(rep["skipped"]) == {"huc12", "county", "regional_board"}
    assert rep["derived"]["county"] == 1    # derive still runs (idempotent)
