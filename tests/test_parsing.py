"""Unit tests for the CSV parsing helpers (no database required)."""

from datetime import date

from fhab.parsing import (
    clean,
    parse_bool,
    parse_data_type,
    parse_date,
    parse_float,
    parse_int,
)


def test_clean_treats_placeholders_as_none():
    assert clean("  Clear Lake ") == "Clear Lake"
    assert clean("") is None
    assert clean("Unknown") is None
    assert clean("N/A") is None
    assert clean(None) is None


def test_parse_bool():
    assert parse_bool("Yes") is True
    assert parse_bool("no") is False
    assert parse_bool("") is None


def test_parse_us_datetime_to_date():
    assert parse_date("5/31/2026 4:24:20 PM") == date(2026, 5, 31)
    assert parse_date("1/1/2020 12:00:00 AM") == date(2020, 1, 1)
    assert parse_date("2026-06-15") == date(2026, 6, 15)
    assert parse_date("not a date") is None


def test_parse_numbers():
    assert parse_int("490") == 490
    assert parse_int("3.0") == 3
    assert parse_int("") is None
    assert parse_float("4.2") == 4.2
    assert parse_float("xyz") is None


def test_parse_data_type():
    assert parse_data_type("Laboratory") == "Laboratory"
    assert parse_data_type("visual") == "Field Visual"
    assert parse_data_type("Veterinary") == "Veterinary"
    assert parse_data_type("") is None
