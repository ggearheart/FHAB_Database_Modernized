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

from .auth import acting_as
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


def load_station_registry(conn: psycopg.Connection, csv_path: Path) -> int:
    """Load the CEDEN station lookup CSV into station_registry. Returns rows loaded."""
    n = 0
    with conn.cursor() as cur:
        cur.execute("TRUNCATE station_registry")
        for row in _rows(csv_path):
            code = clean(row.get("StationCode"))
            if code is None:
                continue
            cur.execute(
                """INSERT INTO station_registry
                     (station_code, station_name, latitude, longitude, datum, source)
                   VALUES (%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (station_code) DO NOTHING""",
                (code, clean(row.get("StationName")),
                 parse_float(row.get("TargetLatitude")), parse_float(row.get("TargetLongitude")),
                 clean(row.get("Datum")), clean(row.get("StationSource"))),
            )
            n += 1
    conn.commit()
    return n


def enrich_station_geom(conn: psycopg.Connection) -> int:
    """Set station.geom from the CEDEN station_registry by station_code. Returns updated count."""
    n = conn.execute(
        """
        UPDATE station s
        SET geom = ST_SetSRID(ST_MakePoint(r.longitude, r.latitude), 4326)
        FROM station_registry r
        WHERE s.station_code = r.station_code
          AND r.latitude IS NOT NULL AND r.longitude IS NOT NULL
          AND s.geom IS NULL
        RETURNING s.id
        """
    ).fetchall()
    conn.commit()
    return len(n)


def link_samples(conn: psycopg.Connection) -> int:
    """Tiered matcher: connect CEDEN samples to FHAB events/cases (best-effort).

    Tier names by station to an FHAB waterbody, then to an event/case within a date window.
    Deterministic key tiers (SampleID/COC, station_code+date) run first where data exists.
    Returns the number of links written. Idempotent: clears prior auto-links first.
    """
    conn.execute("DELETE FROM sample_link WHERE reviewed_by IS NULL")

    # Tier 3 (spatial+temporal): station point within 1 km of an FHAB event location and
    # sample date within 30 days. Runs first; names are the fallback for un-geocoded stations.
    spatial = conn.execute(
        """
        WITH candidate AS (
            SELECT s.id AS sample_id, s.station_id, e.bloom_report_id, e.case_id,
                   ST_Distance(st.geom::geography, l.geom::geography) AS dist_m,
                   abs(s.sample_date - e.observation_date) AS day_gap
            FROM sample s
            JOIN station st ON st.id = s.station_id AND st.geom IS NOT NULL
            JOIN event e ON e.observation_date IS NOT NULL
            JOIN location l ON l.id = e.location_id AND l.geom IS NOT NULL
            WHERE s.sample_date IS NOT NULL
              AND abs(s.sample_date - e.observation_date) <= 30
              AND ST_DWithin(st.geom::geography, l.geom::geography, 1000)
        ),
        best AS (
            SELECT DISTINCT ON (sample_id) * FROM candidate ORDER BY sample_id, dist_m, day_gap
        )
        INSERT INTO sample_link
            (sample_id, station_id, bloom_report_id, case_id, match_method, confidence, distance_m)
        SELECT sample_id, station_id, bloom_report_id, case_id, 'spatial_temporal',
               greatest(0.4, 1.0 - dist_m/1000.0), dist_m
        FROM best
        RETURNING id
        """
    ).fetchall()

    # Tier 4 (name): station_name ~ waterbody name -> event at that waterbody within 30 days.
    # Only for samples not already linked spatially.
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
              AND s.id NOT IN (SELECT sample_id FROM sample_link WHERE sample_id IS NOT NULL)
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
    return len(spatial) + len(linked)


def load_ceden_output(
    conn: psycopg.Connection, field_csv: Path | None, chemistry_csv: Path, link: bool = True
) -> CedenReport:
    """Load CEDEN WaterChemistry (and optional FieldResults) across many stations and link to FHAB.

    `field_csv` is optional — stations are also resolved from the WaterChemistry rows — so a
    batch ingest can run from a chemistry file alone.
    """
    loader = CedenLoader(conn)
    if field_csv:
        loader.load_field_results(field_csv)
    loader.load_water_chemistry(chemistry_csv)
    conn.commit()
    loader.report.counts["stations"] = len(loader._stations)
    # Enrich station geometry from the CEDEN registry (if loaded) so spatial linking works.
    loader.report.counts["geocoded"] = enrich_station_geom(conn)
    if link:
        loader.report.counts["event_links"] = link_samples(conn)
    return loader.report


def _load_chemistry_pinned(conn: psycopg.Connection, user_id: int, chemistry_csv, *,
                           bloom_report_id: int | None = None,
                           case_id: int | None = None) -> CedenReport:
    """Attach a CEDEN WaterChemistry CSV's samples + results to a report or a case.

    Reuses the CEDEN ingest logic (row parsing + analyte taxonomy) but, instead of resolving
    stations and spatially matching, pins every sample to the given report or case. Runs as
    `user_id`, so the staff-write RLS policy on sample/result applies. Idempotent on BG_ID.
    """
    loader = CedenLoader(conn)
    samples: dict[str, int] = {}
    n_results = 0
    with acting_as(conn, user_id):
        for row in _rows(chemistry_csv):
            bg_id = clean(row.get("BG_ID"))
            skey = bg_id or f"{row.get('StationCode')}|{row.get('SampleDate')}"
            if skey not in samples:
                srow = conn.execute(
                    """INSERT INTO sample
                         (bloom_report_id, case_id, sample_date, sample_time, sample_type, bg_id,
                          lab_sample_id, lab_batch, project_code, lab_agency_code)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (bg_id) WHERE bg_id IS NOT NULL
                         DO UPDATE SET bloom_report_id = EXCLUDED.bloom_report_id,
                                       case_id = EXCLUDED.case_id
                       RETURNING id""",
                    (bloom_report_id, case_id, parse_date(row.get("SampleDate")),
                     _parse_time(row.get("SampleTime")), clean(row.get("SampleTypeCode")), bg_id,
                     clean(row.get("LabSampleID")), clean(row.get("LabBatch")),
                     clean(row.get("ProjectCode")), clean(row.get("LabAgencyCode"))),
                ).fetchone()
                samples[skey] = srow["id"]
            analyte_id = loader._analyte_id(row.get("Analyte"), row.get("MethodName"))
            ruid = f"{bg_id or skey}:{clean(row.get('Analyte'))}"
            conn.execute(
                """INSERT INTO result
                     (result_id_unique, sample_id, analyte_id, data_type, method,
                      measurement_value, measurement_unit, res_qual_code, fraction_name, mdl, rl,
                      results_date)
                   VALUES (%s,%s,%s,'Laboratory',%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (result_id_unique) DO UPDATE SET
                     measurement_value = EXCLUDED.measurement_value,
                     res_qual_code = EXCLUDED.res_qual_code""",
                (ruid, samples[skey], analyte_id, clean(row.get("MethodName")),
                 parse_float(row.get("Result")), clean(row.get("Units")),
                 clean(row.get("ResQualCode")), clean(row.get("Fraction")),
                 parse_float(row.get("MDL")), parse_float(row.get("RL")),
                 parse_date(row.get("LabCompletionDate"))),
            )
            n_results += 1
        conn.commit()
    loader.report.counts = {"samples": len(samples), "results": n_results,
                            "analytes": len(loader._analytes)}
    return loader.report


def load_chemistry_for_event(conn, bloom_report_id, chemistry_csv, user_id) -> CedenReport:
    """Attach uploaded CEDEN lab results to one report (event)."""
    return _load_chemistry_pinned(conn, user_id, chemistry_csv, bloom_report_id=bloom_report_id)


def load_chemistry_for_case(conn, case_id, chemistry_csv, user_id) -> CedenReport:
    """Attach uploaded CEDEN lab results to a whole case (not a single report)."""
    return _load_chemistry_pinned(conn, user_id, chemistry_csv, case_id=case_id)
