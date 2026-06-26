"""DB-backed tests for loading the published flat files into the CRM schema."""


def _count(conn, table):
    return conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]


def test_fixtures_load_all_tables(loaded_conn):
    # Referentially-consistent fixtures: 11 events, 9 cases, 12 responses, 12 results.
    assert _count(loaded_conn, "event") == 11
    assert _count(loaded_conn, "hab_case") == 9
    assert _count(loaded_conn, "response") == 12
    assert _count(loaded_conn, "result") == 12
    assert _count(loaded_conn, "waterbody") >= 1


def test_event_is_keyed_by_bloom_report_id(loaded_conn):
    row = loaded_conn.execute(
        "SELECT bloom_report_id FROM event ORDER BY bloom_report_id LIMIT 1"
    ).fetchone()
    assert isinstance(row["bloom_report_id"], int)


def test_response_relates_to_event_or_case(loaded_conn):
    # The CHECK constraint guarantees this, but assert no orphan responses slipped in.
    orphans = loaded_conn.execute(
        "SELECT count(*) AS n FROM response WHERE bloom_report_id IS NULL AND case_id IS NULL"
    ).fetchone()["n"]
    assert orphans == 0


def test_geometry_built_from_lat_long(loaded_conn):
    # At least one event location should have a valid PostGIS point within California-ish bounds.
    row = loaded_conn.execute(
        """SELECT ST_Y(geom) AS lat, ST_X(geom) AS lon
           FROM location WHERE geom IS NOT NULL LIMIT 1"""
    ).fetchone()
    assert row is not None
    assert 30 < row["lat"] < 50
    assert -125 < row["lon"] < -110


def test_result_uses_unique_key_not_repeating_result_id(loaded_conn):
    # result_id_unique is the PK; loading must not drop rows that share a Result_ID.
    n_results = _count(loaded_conn, "result")
    n_unique = loaded_conn.execute(
        "SELECT count(DISTINCT result_id_unique) AS n FROM result"
    ).fetchone()["n"]
    assert n_results == n_unique == _count(loaded_conn, "result")


def test_idempotent_reload(loaded_conn):
    from tests.conftest import FIXTURES
    from fhab.loaders import load_open_data

    load_open_data(loaded_conn, FIXTURES)  # second load
    assert _count(loaded_conn, "event") == 11
    assert _count(loaded_conn, "result") == 12
