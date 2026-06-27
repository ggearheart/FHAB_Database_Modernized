"""Tests for ingesting the Bend->CEDEN workflow output into the FHAB database."""

from pathlib import Path

from fhab.ceden import load_ceden_output, load_station_registry

CEDEN = Path(__file__).parent / "fixtures" / "ceden"
FIELD = CEDEN / "CEDEN_FieldResults.csv"
CHEM = CEDEN / "CEDEN_WaterChemistry.csv"
REGISTRY = CEDEN / "station_registry.csv"


def _count(conn, table):
    return conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]


def test_ceden_load_creates_stations_samples_results(conn):
    rep = load_ceden_output(conn, FIELD, CHEM)
    assert rep.counts["stations"] == 4
    assert rep.counts["samples"] == 4
    assert rep.counts["results"] == 16
    assert rep.counts["analytes"] == 4  # Anatoxin-a, Microcystin, Cylindrospermopsin, Saxitoxin


def test_ceden_fills_measurement_values(conn):
    # The whole point: CEDEN results carry real values where FHAB results were blank.
    load_ceden_output(conn, FIELD, CHEM)
    row = conn.execute(
        """SELECT count(*) AS n, count(measurement_value) AS filled,
                  count(*) FILTER (WHERE res_qual_code = 'ND') AS nd
           FROM result"""
    ).fetchone()
    assert row["n"] == 16
    assert row["filled"] == 16        # every CEDEN result has a numeric value
    assert row["nd"] == 16            # all non-detect in this batch, reported at the RL


def test_ceden_analytes_align_to_taxonomy(conn):
    load_ceden_output(conn, FIELD, CHEM)
    types = {r["analysis_type"] for r in conn.execute(
        "SELECT DISTINCT analysis_type FROM analyte").fetchall()}
    assert "Cyanotoxin" in types   # ELISA -> Cyanotoxin


def test_ceden_load_is_idempotent(conn):
    load_ceden_output(conn, FIELD, CHEM)
    load_ceden_output(conn, FIELD, CHEM)  # second load must not duplicate
    assert _count(conn, "result") == 16
    assert _count(conn, "station") == 4
    assert _count(conn, "sample") == 4


def test_linker_connects_sample_to_matching_event(conn):
    # Seed an FHAB waterbody/location/event matching a CEDEN station, near the sample date.
    wb = conn.execute(
        "INSERT INTO waterbody (water_body_name) VALUES ('Arroyo Hondo Creek') RETURNING id"
    ).fetchone()["id"]
    loc = conn.execute(
        "INSERT INTO location (waterbody_id) VALUES (%s) RETURNING id", (wb,)
    ).fetchone()["id"]
    conn.execute(
        """INSERT INTO event (bloom_report_id, location_id, observation_date)
           VALUES (999001, %s, DATE '2026-06-04')""",
        (loc,),
    )
    conn.commit()

    rep = load_ceden_output(conn, FIELD, CHEM)
    assert rep.counts["event_links"] >= 1

    link = conn.execute(
        """SELECT match_method, bloom_report_id FROM sample_link
           WHERE bloom_report_id = 999001"""
    ).fetchone()
    assert link is not None
    assert link["match_method"] == "name"


def test_linker_leaves_unmatched_routine_samples_unlinked(conn):
    # With no matching FHAB events, routine CEDEN samples load but produce no event links.
    rep = load_ceden_output(conn, FIELD, CHEM)
    assert rep.counts["event_links"] == 0
    assert _count(conn, "sample") == 4  # still ingested as station/monitoring data


def test_station_registry_enriches_geometry(conn):
    n = load_station_registry(conn, REGISTRY)
    assert n == 4
    rep = load_ceden_output(conn, FIELD, CHEM)
    assert rep.counts["geocoded"] == 4   # all 4 stations got coordinates from the registry
    null_geom = conn.execute(
        "SELECT count(*) AS n FROM station WHERE geom IS NULL").fetchone()["n"]
    assert null_geom == 0


def test_ensure_station_registry_idempotent_and_gz(conn, tmp_path):
    import gzip
    from fhab.ceden import ensure_station_registry

    # Loads on an empty registry...
    s1 = ensure_station_registry(conn, REGISTRY)
    assert s1["loaded"] == 4
    # ...and is a no-op the second time (matcher arming is cheap to call every boot).
    s2 = ensure_station_registry(conn, REGISTRY)
    assert s2.get("already") == 4 and s2["loaded"] == 0

    # A .gz registry loads transparently (the form committed for Render).
    conn.execute("TRUNCATE station_registry")
    conn.commit()
    gz = tmp_path / "reg.csv.gz"
    with open(REGISTRY, "rb") as src, gzip.open(gz, "wb") as dst:
        dst.write(src.read())
    s3 = ensure_station_registry(conn, str(gz))
    assert s3["loaded"] == 4


def test_spatial_linker_connects_nearby_event(conn):
    load_station_registry(conn, REGISTRY)
    # Seed an FHAB event ~30 m from the Muddy Hollow Creek station, near the sample date.
    wb = conn.execute(
        "INSERT INTO waterbody (water_body_name) VALUES ('Some Creek') RETURNING id"
    ).fetchone()["id"]
    loc = conn.execute(
        """INSERT INTO location (waterbody_id, geom)
           VALUES (%s, ST_SetSRID(ST_MakePoint(-122.8675, 38.0525), 4326)) RETURNING id""",
        (wb,),
    ).fetchone()["id"]
    conn.execute(
        """INSERT INTO event (bloom_report_id, location_id, observation_date)
           VALUES (900001, %s, %s::date)""",
        (loc, "2026-06-03"),
    )
    conn.commit()

    rep = load_ceden_output(conn, FIELD, CHEM)
    assert rep.counts["event_links"] >= 1
    link = conn.execute(
        "SELECT match_method, distance_m FROM sample_link WHERE bloom_report_id = 900001"
    ).fetchone()
    assert link is not None
    assert link["match_method"] == "spatial_temporal"
    assert link["distance_m"] < 1000


def test_load_chemistry_for_event_attaches_results(conn):
    from fhab.auth import create_user, grant_role
    from fhab.reports import enter_report
    from fhab.ceden import load_chemistry_for_event
    staff = create_user(conn, "labup@wb.ca.gov")
    grant_role(conn, staff, "wb_staff", region="Region 5 - Central Valley")
    rid = enter_report(conn, staff, water_body_name="Lab Upload Lake",
                       region="Region 5 - Central Valley")
    rep = load_chemistry_for_event(conn, rid, CHEM, staff)
    assert rep.counts["samples"] == 4 and rep.counts["results"] == 16
    n = conn.execute(
        """SELECT count(*) c FROM result r JOIN sample s ON s.id=r.sample_id
           WHERE s.bloom_report_id=%s AND r.data_type='Laboratory'""", (rid,)).fetchone()["c"]
    assert n == 16
