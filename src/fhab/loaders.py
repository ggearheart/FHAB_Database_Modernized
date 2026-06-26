"""Load the four published CA FHAB flat files into the normalized schema.

Load order respects foreign keys: cases -> events -> responses (+advisories) -> results.
References to rows not present in the loaded snapshot are nulled out rather than failing,
so a partial/filtered export still loads cleanly.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

import psycopg

from .parsing import (
    clean,
    parse_bool,
    parse_data_type,
    parse_date,
    parse_datetime,
    parse_float,
    parse_int,
)


@dataclass
class LoadReport:
    counts: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        lines = ["loaded:"]
        for k, v in self.counts.items():
            lines.append(f"  {k:12} {v:>7,}")
        if any(self.skipped.values()):
            lines.append("skipped:")
            for k, v in self.skipped.items():
                if v:
                    lines.append(f"  {k:12} {v:>7,}")
        return "\n".join(lines)


def _rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def _g(row: dict, *names: str) -> str | None:
    """Get the first present column among `names`, tolerant of spacing/case variants."""
    for n in names:
        if n in row:
            return row[n]
    lower = {k.lower().replace(" ", "_"): v for k, v in row.items()}
    for n in names:
        key = n.lower().replace(" ", "_")
        if key in lower:
            return lower[key]
    return None


class Loader:
    def __init__(self, conn: psycopg.Connection, data_dir: Path):
        self.conn = conn
        self.data_dir = Path(data_dir)
        self.report = LoadReport()
        self._waterbodies: dict[tuple[str | None, str | None], int] = {}
        self._cases: set[int] = set()
        self._events: set[int] = set()
        self._analytes: dict[tuple, int] = {}

    # ---- helpers ----

    def _waterbody_id(self, name: str | None, county: str | None, **extra) -> int | None:
        name = clean(name)
        if name is None:
            return None
        county = clean(county)
        key = (name, county)
        if key in self._waterbodies:
            return self._waterbodies[key]
        row = self.conn.execute(
            """INSERT INTO waterbody
                 (water_body_name, county, official_water_body_name, water_body_type,
                  regional_water_board, water_body_manager, drinking_water_source)
               VALUES (%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (water_body_name, county) DO UPDATE SET water_body_name = EXCLUDED.water_body_name
               RETURNING id""",
            (name, county, clean(extra.get("official")), clean(extra.get("wb_type")),
             clean(extra.get("region")), clean(extra.get("manager")), clean(extra.get("dw"))),
        ).fetchone()
        self._waterbodies[key] = row["id"]
        return row["id"]

    def _location_id(self, waterbody_id, lat, lon, datum, landmark) -> int | None:
        lat, lon = parse_float(lat), parse_float(lon)
        datum, landmark = clean(datum), clean(landmark)
        if waterbody_id is None and lat is None and lon is None and landmark is None:
            return None
        geom = None
        if lat is not None and lon is not None and (lat != 0 or lon != 0):
            geom = (lon, lat)
        row = self.conn.execute(
            """INSERT INTO location (waterbody_id, geom, bloom_datum, landmark)
               VALUES (%s, CASE WHEN %s::float8 IS NULL THEN NULL
                              ELSE ST_SetSRID(ST_MakePoint(%s,%s),4326) END, %s, %s)
               RETURNING id""",
            (waterbody_id, geom[0] if geom else None,
             geom[0] if geom else None, geom[1] if geom else None, datum, landmark),
        ).fetchone()
        return row["id"]

    def _analyte_id(self, analysis_type, analyte_class, analyte) -> int | None:
        a1, a2, a3 = clean(analysis_type), clean(analyte_class), clean(analyte)
        if a1 is None and a2 is None and a3 is None:
            return None
        key = (a1, a2, a3)
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

    # ---- per-file loaders ----

    def load_cases(self) -> None:
        n = 0
        for row in _rows(self.data_dir / "cases.csv"):
            cid = parse_int(_g(row, "Case_ID"))
            if cid is None or cid == 0 or cid in self._cases:
                continue
            wb = self._waterbody_id(_g(row, "Case_Water_Body_Name", "Water_Body_Name"),
                                    _g(row, "County"))
            self.conn.execute(
                """INSERT INTO hab_case
                     (case_id, waterbody_id, case_water_body_name, case_class, case_status,
                      case_lead, case_year, case_start_date, case_end_date, case_datetimestamp)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (case_id) DO NOTHING""",
                (cid, wb, clean(_g(row, "Case_Water_Body_Name")), clean(_g(row, "Case_Class")),
                 clean(_g(row, "Case_Status")),
                 clean(_g(row, "Case_Lead")), parse_int(_g(row, "Case_Year")),
                 parse_date(_g(row, "Case_Start_Date")), parse_date(_g(row, "Case_End_Date")),
                 parse_datetime(_g(row, "Case_DateTimeStamp"))),
            )
            self._cases.add(cid)
            n += 1
        self.report.counts["cases"] = n

    def load_events(self) -> None:
        n = 0
        for row in _rows(self.data_dir / "bloom_reports.csv"):
            eid = parse_int(_g(row, "Bloom_Report_ID"))
            if eid is None or eid in self._events:
                continue
            wb = self._waterbody_id(
                _g(row, "Water_Body_Name"), _g(row, "County"),
                official=_g(row, "Official_Water_Body_Name"), wb_type=_g(row, "Water_Body_Type"),
                region=_g(row, "Regional_Water_Board"), manager=_g(row, "Water_Body_Manager"),
                dw=_g(row, "Drinking_Water_Source"))
            loc = self._location_id(wb, _g(row, "Bloom_Latitude"), _g(row, "Bloom_Longitude"),
                                    _g(row, "Bloom_Datum"), _g(row, "Landmark"))
            cid = parse_int(_g(row, "Case_ID"))
            cid = cid if cid in self._cases else None
            self.conn.execute(
                """INSERT INTO event
                     (bloom_report_id, case_id, location_id, report_type, observation_date,
                      bloom_date_created, bloom_type, bloom_size, bloom_location, bloom_texture,
                      surface_water_condition, weather_condition, reported_advisory_types, has_pictures)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (bloom_report_id) DO NOTHING""",
                (eid, cid, loc, clean(_g(row, "Report_Type")), parse_date(_g(row, "Observation_Date")),
                 parse_datetime(_g(row, "Bloom_Date_Created")), clean(_g(row, "Bloom_Type")),
                 clean(_g(row, "Bloom_Size")), clean(_g(row, "Bloom_Location")),
                 clean(_g(row, "Bloom_Texture")), clean(_g(row, "Surface_Water_Condition")),
                 clean(_g(row, "Weather_Condition")), clean(_g(row, "Reported_Advisory_Types")),
                 parse_bool(_g(row, "Has_Pictures"))),
            )
            self._events.add(eid)
            n += 1
        self.report.counts["events"] = n

    def load_responses(self) -> None:
        n, skipped, adv = 0, 0, 0
        seen_adv: set[int] = set()
        for row in _rows(self.data_dir / "responses.csv"):
            rid = parse_int(_g(row, "Response_Action_ID"))
            if rid is None:
                continue
            eid = parse_int(_g(row, "Bloom_Report_ID"))
            eid = eid if eid in self._events else None
            cid = parse_int(_g(row, "Case_ID"))
            cid = cid if cid in self._cases else None
            if eid is None and cid is None:
                skipped += 1
                continue
            self.conn.execute(
                """INSERT INTO response
                     (response_action_id, bloom_report_id, case_id, response_category,
                      response_type, response_datetimestamp)
                   VALUES (%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (response_action_id) DO NOTHING""",
                (rid, eid, cid, clean(_g(row, "Response_Category")), clean(_g(row, "Response_Type")),
                 parse_datetime(_g(row, "Response_DateTimeStamp"))),
            )
            n += 1
            aid = parse_int(_g(row, "Advisory_ID"))
            if aid is not None and aid not in seen_adv:
                self.conn.execute(
                    """INSERT INTO advisory
                         (advisory_id, response_action_id, advisory_recommended,
                          advisory_start_date, advisory_end_date, advisory_detail,
                          spatial_extent_of_advisory, extent_unit_of_measure,
                          display_advisory_on_map, advisory_date_of_recommendation, advisory_date)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (advisory_id) DO NOTHING""",
                    (aid, rid, clean(_g(row, "Advisory_Recommended")),
                     parse_date(_g(row, "Advisory_Start_Date")), parse_date(_g(row, "Advisory_End_Date")),
                     clean(_g(row, "Advisory_Detail")), parse_float(_g(row, "Spatial_Extent_of_Advisory")),
                     clean(_g(row, "Extent_Unit_of_Measure")), parse_bool(_g(row, "DisplayAdvisoryToMap")),
                     parse_date(_g(row, "Advisory_Date_of_Recommendation")),
                     parse_datetime(_g(row, "Advisory_Date"))),
                )
                seen_adv.add(aid)
                adv += 1
        self.report.counts["responses"] = n
        self.report.counts["advisories"] = adv
        self.report.skipped["responses"] = skipped

    def load_results(self) -> None:
        n = 0
        for row in _rows(self.data_dir / "results.csv"):
            res_uid = clean(_g(row, "RESULT ID UNIQUE", "RESULT_ID_UNIQUE"))
            if res_uid is None:
                continue
            res_id = parse_int(_g(row, "Result_ID"))
            eid = parse_int(_g(row, "Bloom_Report_ID"))
            eid = eid if eid in self._events else None
            cid = parse_int(_g(row, "Case_ID"))
            cid = cid if cid in self._cases else None
            srow = self.conn.execute(
                """INSERT INTO sample
                     (bloom_report_id, case_id, sample_id, sample_type, sample_location, site, sample_date)
                   VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (eid, cid, clean(_g(row, "Sample_ID")), clean(_g(row, "Sample_Type")),
                 clean(_g(row, "Sample_Location")), clean(_g(row, "Site")),
                 parse_date(_g(row, "Sample_Date"))),
            ).fetchone()
            analyte_id = self._analyte_id(_g(row, "Analysis_Type"), _g(row, "Analyte_Class"),
                                          _g(row, "Analyte"))
            mval = parse_float(_g(row, "Measurement_Value"))
            self.conn.execute(
                """INSERT INTO result
                     (result_id_unique, result_id, sample_id, analyte_id, data_type,
                      measurement_type, method, measurement_value, measurement_text,
                      measurement_unit, taxa, results_date)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (result_id_unique) DO NOTHING""",
                (res_uid, res_id, srow["id"], analyte_id, parse_data_type(_g(row, "Data_Type")),
                 clean(_g(row, "Measurement_Type")), clean(_g(row, "Method")), mval,
                 clean(_g(row, "Measurement_Value")) if mval is None else None,
                 clean(_g(row, "Measurement_Unit")), clean(_g(row, "Taxa")),
                 parse_date(_g(row, "Results_Date"))),
            )
            n += 1
        self.report.counts["results"] = n
        self.report.counts["analytes"] = len(self._analytes)

    def load_all(self) -> LoadReport:
        self.load_cases()
        self.load_events()
        self.load_responses()
        self.load_results()
        self.conn.commit()
        return self.report


def load_open_data(conn: psycopg.Connection, data_dir: Path) -> LoadReport:
    """Load all four published flat files from `data_dir` into the schema."""
    return Loader(conn, data_dir).load_all()
