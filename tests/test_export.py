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
    for col in ("ResultRowID", "Bloom_Report_ID", "Case_ID", "HUC12", "Latitude",
                "Station_GeoConnex"):
        assert col in xw_h
    # Same row population and a shared join key (ResultRowID) across both files.
    assert len(chem) == len(xw)
    assert {r["ResultRowID"] for r in chem} == {r["ResultRowID"] for r in xw}


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
