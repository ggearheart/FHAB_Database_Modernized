"""Lightweight, dependency-free validation for incoming FHAB records."""

from __future__ import annotations

from datetime import date

ADVISORY_TIERS = {"caution", "warning", "danger"}
DETECT_FLAGS = {"detect", "non-detect", "estimated", ""}


def is_iso_date(value: str) -> bool:
    """True if value is a valid ISO-8601 date (YYYY-MM-DD)."""
    try:
        date.fromisoformat(value)
        return True
    except (ValueError, TypeError):
        return False


def validate_sample(record: dict) -> list[str]:
    """Return a list of human-readable problems with a sample record. Empty == valid."""
    errors: list[str] = []

    if not record.get("site_name"):
        errors.append("missing site_name")

    sample_date = record.get("sample_date", "")
    if not is_iso_date(sample_date):
        errors.append(f"invalid sample_date: {sample_date!r} (expected YYYY-MM-DD)")

    value = record.get("value")
    if value not in (None, "") and float(value) < 0:
        errors.append(f"negative result value: {value}")

    flag = (record.get("detect_flag") or "").lower()
    if flag not in DETECT_FLAGS:
        errors.append(f"unknown detect_flag: {flag!r}")

    return errors
