"""Ingest the Bend->CEDEN workflow output into the FHAB database.

The `Bend_CEDEN_workflow` tool (https://github.com/ggearheart/Bend_CEDEN_workflow) emits
two CSVs we consume here:

- CEDEN_FieldResults_*.csv  -> station visits / field metadata
- CEDEN_WaterChemistry_*.csv -> long-format chemistry results

This loader resolves stations, creates samples + results (filling the analyte values that
are blank in the FHAB published data), and runs a tiered matcher to link each sample to an
FHAB event/case. See docs/BEND_CEDEN_WORKFLOW.md.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

import psycopg

from .parsing import clean, parse_date, parse_float

# Method -> analysis_type, to align CEDEN analytes with the existing analyte taxonomy.
_METHOD_ANALYSIS_TYPE = {
    "ELISA": "Cyanotoxin",
    "qPCR": "Genetic",
    "Spectrophotometry": "Pigment",
}


@dataclass
class CedenReport:
    counts: dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        return "loaded (CEDEN):\n" + "\n".join(
            f"  {k:14} {v:>6,}" for k, v in self.counts.items()
        )


def _rows(path: Path) -> list[dict]:
    with Path(path).open(newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def _parse_time(value: str | None) -> str | None:
    v = clean(value)
    if v is None:
        return None
    # CEDEN SampleTime is HH:MM (24h); store as a time literal.
    return v if len(v) >= 4 else None


class CedenLoader:
    def __init__(self, conn: psycopg.Connection):
        self.conn = conn
        self.report = CedenReport()
        self._stations: dict[str, int] = {}
        self._analytes: dict[tuple, int] = {}

    def _station_id(self, code: str | None, name: str | None) -> int | None:
        code = clean(code)
        if code is None:
            return None
        if code in self._stations:
            return self._stations[code]
        row = self.conn.execute(
            """INSERT INTO station (station_code, station_name)
               VALUES (%s, %s)
               ON CONFLICT (station_code)
                 DO UPDATE SET station_name = COALESCE(EXCLUDED.station_name, station.station_name)
               RETURNING id""",
            (code, clean(name)),
        ).fetchone()
        self._stations[code] = row["id"]
        return row["id"]

    def _analyte_id(self, analyte: str | None, method: str | None) -> int | None:
        a = clean(analyte)
        if a is None:
            return None
        analysis_type = _METHOD_ANALYSIS_TYPE.get(clean(method) or "", None)
        key = (analysis_type, None, a)
        if key in self._analytes:
            return self._analytes[key]
        row = self.conn.execute(
            """INSERT INTO analyte (analysis_type, analyte_class, analyte)
               VALUES (%s,%s,%s)
               ON CONFLICT (analysis_type, analyte_class, analyte) DO UPDATE SET analyte = EXCLUDED.analyte
               RETURNING id""",
            key,
        ).fetchone()
        self._analytes[key] = row["id"]
        return row["id"]

    def load_field_results(self, path: Path) -> None:
        n = 0
        for row in _rows(path):
            if self._station_id(row.get("StationCode"), row.get("StationName")) is not None:
                n += 1
        self.report.counts["stations"] = len(self._stations)

    def load_water_chemistry(self, path: Path) -> None:
        samples: dict[str, int] = {}      # bg_id (or station|date) -> sample.id
        n_results = 0
        for row in _rows(path):
            station_id = self._station_id(row.get("StationCode"), row.get("StationName"))
            sample_date = parse_date(row.get("SampleDate"))
            bg_id = clean(row.get("BG_ID"))
            skey = bg_id or f"{row.get('StationCode')}|{row.get('SampleDate')}"
            if skey not in samples:
                # Upsert on bg_id so re-loading a batch converges (idempotent).
                srow = self.conn.execute(
                    """INSERT INTO sample
                         (station_id, sample_date, sample_time, sample_type, bg_id,
                          lab_sample_id, lab_batch, project_code, lab_agency_code)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (bg_id) WHERE bg_id IS NOT NULL
                         DO UPDATE SET station_id = EXCLUDED.station_id
                       RETURNING id""",
                    (station_id, sample_date, _parse_time(row.get("SampleTime")),
                     clean(row.get("SampleTypeCode")), bg_id, clean(row.get("LabSampleID")),
                     clean(row.get("LabBatch")), clean(row.get("ProjectCode")),
                     clean(row.get("LabAgencyCode"))),
                ).fetchone()
                samples[skey] = srow["id"]
            analyte_id = self._analyte_id(row.get("Analyte"), row.get("MethodName"))
            # result_id_unique: BG_ID + analyte is unique within a batch.
            ruid = f"{bg_id or skey}:{clean(row.get('Analyte'))}"
            self.conn.execute(
                """INSERT INTO result
                     (result_id_unique, sample_id, analyte_id, data_type, method,
                      measurement_value, measurement_unit, res_qual_code, fraction_name,
                      mdl, rl, qa_code, compliance_code, results_date)
                   VALUES (%s,%s,%s,'Laboratory',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (result_id_unique) DO UPDATE SET
                     measurement_value = EXCLUDED.measurement_value,
                     res_qual_code = EXCLUDED.res_qual_code""",
                (ruid, samples[skey], analyte_id, clean(row.get("MethodName")),
                 parse_float(row.get("Result")), clean(row.get("Units")),
                 clean(row.get("ResQualCode")), clean(row.get("Fraction")),
                 parse_float(row.get("MDL")), parse_float(row.get("RL")),
                 clean(row.get("QACode")), clean(row.get("ComplianceCode")),
                 parse_date(row.get("LabCompletionDate"))),
            )
            n_results += 1
        self.report.counts["samples"] = len(samples)
        self.report.counts["results"] = n_results
        self.report.counts["analytes"] = len(self._analytes)


def link_samples(conn: psycopg.Connection) -> int:
    """Tiered matcher: connect CEDEN samples to FHAB events/cases (best-effort).

    Tier names by station to an FHAB waterbody, then to an event/case within a date window.
    Deterministic key tiers (SampleID/COC, station_code+date) run first where data exists.
    Returns the number of links written. Idempotent: clears prior auto-links first.
    """
    conn.execute("DELETE FROM sample_link WHERE reviewed_by IS NULL")

    # Tier 4 (name): station_name ~ waterbody name -> event at that waterbody within 30 days.
    linked = conn.execute(
        """
        WITH candidate AS (
            SELECT s.id AS sample_id, s.station_id, e.bloom_report_id, e.case_id,
                   abs(s.sample_date - e.observation_date) AS day_gap
            FROM sample s
            JOIN station st ON st.id = s.station_id
            JOIN waterbody w ON lower(w.water_body_name) = lower(st.station_name)
            JOIN location l ON l.waterbody_id = w.id
            JOIN event e ON e.location_id = l.id
            WHERE s.station_id IS NOT NULL AND s.sample_date IS NOT NULL
              AND e.observation_date IS NOT NULL
              AND abs(s.sample_date - e.observation_date) <= 30
        ),
        best AS (
            SELECT DISTINCT ON (sample_id) sample_id, station_id, bloom_report_id, case_id, day_gap
            FROM candidate ORDER BY sample_id, day_gap
        )
        INSERT INTO sample_link
            (sample_id, station_id, bloom_report_id, case_id, match_method, confidence)
        SELECT sample_id, station_id, bloom_report_id, case_id, 'name',
               greatest(0.3, 1.0 - day_gap/30.0)
        FROM best
        RETURNING id
        """
    ).fetchall()
    conn.commit()
    return len(linked)


def load_ceden_output(
    conn: psycopg.Connection, field_csv: Path, chemistry_csv: Path, link: bool = True
) -> CedenReport:
    """Load a Bend->CEDEN output pair (FieldResults + WaterChemistry) and link to FHAB."""
    loader = CedenLoader(conn)
    loader.load_field_results(field_csv)
    loader.load_water_chemistry(chemistry_csv)
    conn.commit()
    if link:
        loader.report.counts["event_links"] = link_samples(conn)
    return loader.report
