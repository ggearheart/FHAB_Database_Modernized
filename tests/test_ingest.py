import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from fhab.db import connect, init_db
from fhab.ingest import ingest_csv

FIXTURE = Path(__file__).parent / "fixtures" / "sample_incidents.csv"


@pytest.fixture()
def conn():
    c = connect(":memory:")
    init_db(c)
    yield c
    c.close()


def test_ingest_sample_fixture(conn):
    report = ingest_csv(conn, FIXTURE)

    assert report.ok
    assert report.total_rows == 6
    assert report.inserted_results == 6

    # Two distinct waterbodies share the name-level dedupe; Clear Lake has two sites.
    assert conn.execute("SELECT COUNT(*) FROM waterbody").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM site").fetchone()[0] == 4
    assert conn.execute("SELECT COUNT(*) FROM sample").fetchone()[0] == 4
    assert conn.execute("SELECT COUNT(*) FROM result").fetchone()[0] == 6


def test_ingest_is_idempotent(conn):
    ingest_csv(conn, FIXTURE)
    ingest_csv(conn, FIXTURE)  # second run must not duplicate

    assert conn.execute("SELECT COUNT(*) FROM result").fetchone()[0] == 6
    assert conn.execute("SELECT COUNT(*) FROM sample").fetchone()[0] == 4


def test_missing_required_column_raises(conn, tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("waterbody,site_name\nClear Lake,Shore\n")
    with pytest.raises(ValueError, match="missing required columns"):
        ingest_csv(conn, bad)


def test_invalid_rows_are_skipped(conn, tmp_path):
    csv_path = tmp_path / "mixed.csv"
    csv_path.write_text(
        "waterbody,site_name,sample_date,analyte,value,detect_flag\n"
        "Clear Lake,Shore,2026-06-10,microcystin,4.2,detect\n"
        "Clear Lake,Shore,June 10,microcystin,4.2,detect\n"  # bad date -> skipped
    )
    report = ingest_csv(conn, csv_path)
    assert report.total_rows == 2
    assert report.inserted_results == 1
    assert len(report.skipped) == 1
    assert report.skipped[0][0] == 3  # row number (header is line 1)
