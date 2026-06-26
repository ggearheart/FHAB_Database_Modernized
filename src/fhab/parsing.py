"""Parsing helpers for the messy published CSV values (blanks, US dates, yes/no)."""

from __future__ import annotations

from datetime import date, datetime

_MISSING = {"", "na", "n/a", "none", "null", "unknown"}


def clean(value: str | None) -> str | None:
    """Trim; treat blank/placeholder strings as None."""
    if value is None:
        return None
    value = value.strip()
    if value.lower() in _MISSING:
        return None
    return value


def parse_bool(value: str | None) -> bool | None:
    """Yes/No/True/False → bool; blank → None."""
    v = clean(value)
    if v is None:
        return None
    return v.lower() in {"yes", "y", "true", "t", "1"}


# The published files use US datetimes like "5/31/2026 4:24:20 PM" or "1/1/2020 12:00:00 AM".
_DT_FORMATS = (
    "%m/%d/%Y %I:%M:%S %p",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
)


def parse_datetime(value: str | None) -> datetime | None:
    v = clean(value)
    if v is None:
        return None
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            continue
    return None


def parse_date(value: str | None) -> date | None:
    dt = parse_datetime(value)
    return dt.date() if dt else None


def parse_int(value: str | None) -> int | None:
    v = clean(value)
    if v is None:
        return None
    try:
        return int(float(v))
    except ValueError:
        return None


def parse_float(value: str | None) -> float | None:
    v = clean(value)
    if v is None:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def parse_data_type(value: str | None) -> str | None:
    """Map published Data_Type/RESULTS DATA TYPE to the data_type_enum, or None."""
    v = clean(value)
    if v is None:
        return None
    canon = {
        "laboratory": "Laboratory",
        "lab": "Laboratory",
        "field visual": "Field Visual",
        "visual": "Field Visual",
        "field measurement": "Field Measurement",
        "field batch": "Field Batch",
        "lab batch": "Lab Batch",
        "batch": "Lab Batch",
        "veterinary": "Veterinary",
    }
    return canon.get(v.lower())
