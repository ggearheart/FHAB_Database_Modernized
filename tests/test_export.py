"""Round-trip tests: load the published flat files, re-export, compare the modeled core."""

import csv
from pathlib import Path

from fhab.export import export_all
from tests.conftest import FIXTURES


def _ids(path: Path, column: str) -> set[str]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return {r[column].strip() for r in csv.DictReader(fh) if r.get(column, "").strip()}


def test_export_writes_four_files(loaded_conn, tmp_path):
    counts = export_all(loaded_conn, tmp_path)
    assert set(counts) == {
        "bloom-report.csv", "hab-cases.csv", "hab-responses.csv", "hab-results.csv",
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
