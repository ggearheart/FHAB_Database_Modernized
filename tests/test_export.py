"""Round-trip tests: load the published flat files, re-export, compare the modeled core."""

import csv
from pathlib import Path

from fhab.export import export_all
from tests.conftest import FIXTURES


def _ids(path: Path, column: str) -> set[str]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return {r[column].strip() for r in csv.DictReader(fh) if r.get(column, "").strip()}


def test_export_writes_all_files(loaded_conn, tmp_path):
    counts = export_all(loaded_conn, tmp_path)
    assert set(counts) == {
        "bloom-report.csv", "hab-cases.csv", "hab-responses.csv", "hab-results.csv",
        "chemistry-results.csv", "chemistry-crosswalk.csv",
    }
    for name in counts:
        assert (tmp_path / name).exists()


def test_roundtrip_preserves_bloom_report_ids(loaded_conn, tmp_path):
    export_all(loaded_conn, tmp_path)
    original = _ids(FIXTURES / "bloom_reports.csv", "Bloom_Report_ID")
    exported = _ids(tmp_path / "bloom-report.csv", "Bloom_Report_ID")
    assert exported == original


def test_roundtrip_preserves_result_unique_ids(loaded_conn, tmp_path):
    export_all(loaded_conn, tmp_path)
    original = _ids(FIXTURES / "results.csv", "RESULT ID UNIQUE")
    exported = _ids(tmp_path / "hab-results.csv", "RESULT ID UNIQUE")
    assert exported == original


def test_export_headers_use_published_names(loaded_conn, tmp_path):
    export_all(loaded_conn, tmp_path)
    with (tmp_path / "hab-results.csv").open(encoding="utf-8-sig") as fh:
        header = next(csv.reader(fh))
    assert "RESULT ID UNIQUE" in header
    assert "Analyte" in header
    assert "Bloom_Report_ID" in header


def test_fetch_flatfile_returns_records_without_pii(loaded_conn):
    from fhab.export import DATASETS, fetch_flatfile
    assert set(DATASETS) == {"bloom-report", "hab-cases", "hab-responses", "hab-results",
                             "chemistry-results", "chemistry-crosswalk"}
    headers, records = fetch_flatfile(loaded_conn, "bloom-report")
    assert "Bloom_Report_ID" in headers and records and isinstance(records[0], dict)
    # The published column set must never include reporter contact / illness.
    joined = " ".join(headers).lower()
    assert "reporter" not in joined and "illness" not in joined and "email" not in joined


def test_chemistry_and_crosswalk_exports(loaded_conn):
    from fhab.export import fetch_flatfile
    chem_h, chem = fetch_flatfile(loaded_conn, "chemistry-results")
    # CEDEN-structured columns, no PII.
    for col in ("ResultRowID", "StationCode", "AnalyteName", "Result", "ResQualCode",
                "TargetLatitude", "MatrixName"):
        assert col in chem_h
    joined = " ".join(chem_h).lower()
    assert "reporter" not in joined and "illness" not in joined

    xw_h, xw = fetch_flatfile(loaded_conn, "chemistry-crosswalk")
    for col in ("ResultRowID", "Sample_ID", "Sampling_Event_ID", "Bloom_Report_ID", "Case_ID",
                "HUC12", "Latitude", "Station_GeoConnex"):
        assert col in xw_h
    # Same row population and a shared join key (ResultRowID) across both files.
    assert len(chem) == len(xw)
    assert {r["ResultRowID"] for r in chem} == {r["ResultRowID"] for r in xw}
    # Sample_ID is populated and groups a sample's analyte rows (>=1 sample has multiple results).
    assert all(r["Sample_ID"] for r in xw)
    assert len({r["Sample_ID"] for r in xw}) <= len(xw)


def test_matrix_and_datum_in_chemistry_export(conn):
    from fhab.auth import create_user, grant_role
    from fhab.export import fetch_flatfile
    from fhab.reports import enter_report
    staff = create_user(conn, "mx@wb.ca.gov")
    grant_role(conn, staff, "wb_staff", region="Region 5 - Central Valley")
    brid = enter_report(conn, staff, water_body_name="Mx Lake", region="Region 5 - Central Valley")
    st = conn.execute("""INSERT INTO station (station_code, geom, datum)
                         VALUES ('MX1', ST_SetSRID(ST_MakePoint(-121,38),4326), 'NAD83')
                         RETURNING id""").fetchone()["id"]
    sid = conn.execute("INSERT INTO sample (bloom_report_id, station_id) VALUES (%s,%s) RETURNING id",
                       (brid, st)).fetchone()["id"]
    conn.execute("INSERT INTO result (result_id_unique, sample_id, data_type, matrix_name) "
                 "VALUES ('mx-a', %s, 'Laboratory', 'sediment')", (sid,))
    conn.execute("INSERT INTO result (result_id_unique, sample_id, data_type) "
                 "VALUES ('mx-b', %s, 'Laboratory')", (sid,))   # null matrix -> default
    conn.commit()
    by = {r["ResultRowID"]: r for r in fetch_flatfile(conn, "chemistry-results")[1]}
    assert by["mx-a"]["MatrixName"] == "sediment"        # real captured value
    assert by["mx-b"]["MatrixName"] == "samplewater"     # fallback when null
    assert by["mx-a"]["Datum"] == "NAD83"                # real station datum


def test_crosswalk_authoritative_geo_fill(conn):
    """An unlinked lab sample gets County / HUC12 / Region / Water_Body_Name from the boundary
    layers by point-in-polygon — not from any linked report."""
    from pathlib import Path

    from fhab.export import fetch_flatfile
    from fhab.geo import derive_geo, load_counties, load_huc12, load_regional_boards
    fx = Path(__file__).parent / "fixtures" / "geo"
    load_huc12(conn, fx / "huc12_sample.geojson")
    load_counties(conn, fx / "county_sample.geojson")
    load_regional_boards(conn, fx / "regional_board_sample.geojson")
    # a station inside all three fixture polygons, with an unlinked result
    st = conn.execute("""INSERT INTO station (station_code, geom)
                         VALUES ('XW1', ST_SetSRID(ST_MakePoint(-122.8675, 38.0525),4326))
                         RETURNING id""").fetchone()["id"]
    sid = conn.execute("INSERT INTO sample (station_id) VALUES (%s) RETURNING id", (st,)).fetchone()["id"]
    conn.execute("INSERT INTO result (result_id_unique, sample_id, data_type) "
                 "VALUES ('xw-a', %s, 'Laboratory')", (sid,))
    conn.commit()
    derive_geo(conn)

    row = next(r for r in fetch_flatfile(conn, "chemistry-crosswalk")[1] if r["ResultRowID"] == "xw-a")
    assert row["Bloom_Report_ID"] in (None, "")           # not linked to any report
    assert row["County"] == "Test County"                 # authoritative point-in-polygon fill
    assert row["Regional_Water_Board"] == "Region 5 - Central Valley"
    assert str(row["HUC12"]).strip() == "180500059999"
    assert row["Water_Body_Name"] == "Test Watershed"     # WBD subwatershed name fallback
