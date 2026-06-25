"""CSV ingestion into the normalized FHAB schema.

The expected input is a "long" CSV where each row is one analyte result tied to a
sampling event. Rows are upserted so re-running an ingest is idempotent.

Expected columns (extra columns are ignored):

    waterbody, waterbody_type, county, state,
    site_name, latitude, longitude,
    sample_date, collected_by,
    analyte, value, unit, detect_flag

Only ``waterbody``, ``site_name``, ``sample_date``, and ``analyte`` are required;
the rest may be blank.
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .validation import validate_sample

REQUIRED_COLUMNS = {"waterbody", "site_name", "sample_date", "analyte"}


@dataclass
class IngestReport:
    """Summary of an ingest run."""

    total_rows: int = 0
    inserted_results: int = 0
    skipped: list[tuple[int, list[str]]] = field(default_factory=list)  # (row_number, errors)

    @property
    def ok(self) -> bool:
        return not self.skipped

    def summary(self) -> str:
        lines = [
            f"rows read:      {self.total_rows}",
            f"results stored: {self.inserted_results}",
            f"rows skipped:   {len(self.skipped)}",
        ]
        for row_num, errors in self.skipped:
            lines.append(f"  - row {row_num}: {'; '.join(errors)}")
        return "\n".join(lines)


def _clean(value: str | None) -> str | None:
    """Trim whitespace; turn empty strings into None."""
    if value is None:
        return None
    value = value.strip()
    return value or None


def _upsert_waterbody(conn: sqlite3.Connection, row: dict) -> int:
    name = _clean(row.get("waterbody"))
    county = _clean(row.get("county"))
    state = _clean(row.get("state")) or "CA"
    cur = conn.execute(
        "SELECT id FROM waterbody WHERE name = ? AND IFNULL(county,'') = IFNULL(?,'') AND state = ?",
        (name, county, state),
    )
    existing = cur.fetchone()
    if existing:
        return existing["id"]
    cur = conn.execute(
        "INSERT INTO waterbody (name, waterbody_type, county, state) VALUES (?, ?, ?, ?)",
        (name, _clean(row.get("waterbody_type")), county, state),
    )
    return cur.lastrowid


def _upsert_site(conn: sqlite3.Connection, waterbody_id: int, row: dict) -> int:
    name = _clean(row.get("site_name"))
    cur = conn.execute(
        "SELECT id FROM site WHERE waterbody_id = ? AND name = ?", (waterbody_id, name)
    )
    existing = cur.fetchone()
    if existing:
        return existing["id"]
    lat = _clean(row.get("latitude"))
    lon = _clean(row.get("longitude"))
    cur = conn.execute(
        "INSERT INTO site (waterbody_id, name, latitude, longitude) VALUES (?, ?, ?, ?)",
        (waterbody_id, name, float(lat) if lat else None, float(lon) if lon else None),
    )
    return cur.lastrowid


def _upsert_sample(conn: sqlite3.Connection, site_id: int, row: dict, source: str) -> int:
    sample_date = _clean(row.get("sample_date"))
    cur = conn.execute(
        "SELECT id FROM sample WHERE site_id = ? AND sample_date = ?", (site_id, sample_date)
    )
    existing = cur.fetchone()
    if existing:
        return existing["id"]
    cur = conn.execute(
        "INSERT INTO sample (site_id, sample_date, collected_by, source) VALUES (?, ?, ?, ?)",
        (site_id, sample_date, _clean(row.get("collected_by")), source),
    )
    return cur.lastrowid


def _upsert_result(conn: sqlite3.Connection, sample_id: int, row: dict) -> bool:
    """Insert or update one analyte result. Returns True if a row was written."""
    analyte = _clean(row.get("analyte"))
    value = _clean(row.get("value"))
    conn.execute(
        """
        INSERT INTO result (sample_id, analyte, value, unit, detect_flag)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (sample_id, analyte) DO UPDATE SET
            value = excluded.value,
            unit = excluded.unit,
            detect_flag = excluded.detect_flag
        """,
        (
            sample_id,
            analyte,
            float(value) if value is not None else None,
            _clean(row.get("unit")),
            _clean(row.get("detect_flag")),
        ),
    )
    return True


def ingest_csv(conn: sqlite3.Connection, csv_path: Path | str) -> IngestReport:
    """Ingest a long-format CSV into the FHAB schema. Commits on success."""
    csv_path = Path(csv_path)
    report = IngestReport()

    with csv_path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"CSV is missing required columns: {sorted(missing)}")

        # Row numbering starts at 2 to account for the header line.
        for row_num, row in enumerate(reader, start=2):
            report.total_rows += 1
            errors = validate_sample(row)
            if errors:
                report.skipped.append((row_num, errors))
                continue

            waterbody_id = _upsert_waterbody(conn, row)
            site_id = _upsert_site(conn, waterbody_id, row)
            sample_id = _upsert_sample(conn, site_id, row, source=csv_path.name)
            if _upsert_result(conn, sample_id, row):
                report.inserted_results += 1

    conn.commit()
    return report
