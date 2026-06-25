import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fhab.validation import is_iso_date, validate_sample


def test_is_iso_date():
    assert is_iso_date("2026-06-25")
    assert not is_iso_date("06/25/2026")
    assert not is_iso_date("")
    assert not is_iso_date("not-a-date")


def test_valid_sample_has_no_errors():
    record = {
        "site_name": "North Shore",
        "sample_date": "2026-06-25",
        "value": "3.2",
        "detect_flag": "detect",
    }
    assert validate_sample(record) == []


def test_invalid_sample_collects_errors():
    record = {
        "site_name": "",
        "sample_date": "June 25",
        "value": "-1",
        "detect_flag": "maybe",
    }
    errors = validate_sample(record)
    assert len(errors) == 4
