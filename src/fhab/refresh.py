"""Refresh the schema from the authoritative CA FHAB flat files on data.ca.gov.

The open-data *export* publishes our records outward; this module pulls the published
State Water Board files back in and **upserts** them — inserting newly published records and
updating the published fields of existing ones — so a schema seeded from an older snapshot can
be brought current. It is deliberately *responsible*:

- **Upsert, never delete.** Records present locally but absent from the pull are left alone.
- **Preserve local-only fields.** Only columns that come from the published files are updated;
  locally-entered data (suspected illness, assignments, QA, lab links, determinations) is kept.
- **Dry-run preview.** `dry_run=True` computes exactly what would change (new / updated per
  table) and rolls back, so an admin can review before applying.

Reuses the flat-file parsing + reference-resolution in `fhab.loaders`; only the write behavior
(upsert + counting) differs, so seeding/tests (insert-only `Loader`) are untouched.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import psycopg

from .loaders import Loader, LoadReport, _g, _rows
from .parsing import (clean, parse_bool, parse_data_type, parse_date, parse_datetime,
                      parse_float, parse_int)

API = "https://data.ca.gov/api/3/action/resource_show"
DATASET_URL = "https://data.ca.gov/dataset/surface-water-freshwater-harmful-algal-blooms"

# (local filename, stable CKAN resource id) — the four published FHAB flat files, in load order.
RESOURCES = [
    ("cases.csv", "67648948-034f-4882-bbc0-c07c7d38daf9"),
    ("bloom_reports.csv", "c6a36b91-ad38-4611-8750-87ee99e497dd"),
    ("responses.csv", "4283c060-c22f-48f5-a75c-8bccf0c54a99"),
    ("results.csv", "9d4e1df4-0cd6-4165-9e63-effcafd9dccc"),
]


class RefreshError(RuntimeError):
    pass


def _curl(url: str) -> str:
    # The portal's WAF blocks Python's urllib; curl is served. (Same as scripts/fetch_reference_data.)
    if not shutil.which("curl"):
        raise RefreshError("curl is required to reach data.ca.gov but was not found on PATH.")
    try:
        return subprocess.run(["curl", "-fsSL", url], check=True, capture_output=True,
                              text=True, timeout=120).stdout
    except subprocess.CalledProcessError as exc:
        raise RefreshError(f"data.ca.gov request failed: {url}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RefreshError("data.ca.gov request timed out.") from exc


def fetch_published(dest_dir: Path) -> dict[str, Path]:
    """Download the published flat files into `dest_dir`. Returns {filename: path}."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    out: dict[str, Path] = {}
    for name, resource_id in RESOURCES:
        url = json.loads(_curl(f"{API}?id={resource_id}"))["result"]["url"]
        path = dest_dir / name
        subprocess.run(["curl", "-fsSL", "-o", str(path), url], check=True,
                       capture_output=True, timeout=300)
        out[name] = path
    return out


class RefreshLoader(Loader):
    """Insert-or-update variant of the flat-file Loader, counting new vs updated per table."""

    def __init__(self, conn, data_dir):
        super().__init__(conn, data_dir)
        self.report.inserted = {}
        self.report.updated = {}

    def _bump(self, table: str, inserted: bool) -> None:
        d = self.report.inserted if inserted else self.report.updated
        d[table] = d.get(table, 0) + 1

    def _exists(self, table: str, keycol: str, val) -> bool:
        return bool(self.conn.execute(
            f"SELECT 1 FROM {table} WHERE {keycol} = %s", (val,)).fetchone())

    def load_cases(self) -> None:
        for row in _rows(self.data_dir / "cases.csv"):
            cid = parse_int(_g(row, "Case_ID"))
            if cid is None or cid == 0:
                continue
            wb = self._waterbody_id(_g(row, "Case_Water_Body_Name", "Water_Body_Name"),
                                    _g(row, "County"))
            new = not self._exists("hab_case", "case_id", cid)
            vals = (wb, clean(_g(row, "Case_Water_Body_Name")), clean(_g(row, "Case_Class")),
                    clean(_g(row, "Case_Status")), clean(_g(row, "Case_Lead")),
                    parse_int(_g(row, "Case_Year")), parse_date(_g(row, "Case_Start_Date")),
                    parse_date(_g(row, "Case_End_Date")), parse_datetime(_g(row, "Case_DateTimeStamp")))
            if new:
                self.conn.execute(
                    """INSERT INTO hab_case (case_id, waterbody_id, case_water_body_name, case_class,
                         case_status, case_lead, case_year, case_start_date, case_end_date,
                         case_datetimestamp) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", (cid, *vals))
            else:
                self.conn.execute(
                    """UPDATE hab_case SET waterbody_id=%s, case_water_body_name=%s, case_class=%s,
                         case_status=%s, case_lead=%s, case_year=%s, case_start_date=%s,
                         case_end_date=%s, case_datetimestamp=%s WHERE case_id=%s""", (*vals, cid))
            self._cases.add(cid)
            self._bump("cases", new)

    def load_events(self) -> None:
        for row in _rows(self.data_dir / "bloom_reports.csv"):
            eid = parse_int(_g(row, "Bloom_Report_ID"))
            if eid is None:
                continue
            wb = self._waterbody_id(
                _g(row, "Water_Body_Name"), _g(row, "County"),
                official=_g(row, "Official_Water_Body_Name"), wb_type=_g(row, "Water_Body_Type"),
                region=_g(row, "Regional_Water_Board"), manager=_g(row, "Water_Body_Manager"),
                dw=_g(row, "Drinking_Water_Source"))
            cid = parse_int(_g(row, "Case_ID"))
            cid = cid if cid in self._cases else None
            lat, lon = _g(row, "Bloom_Latitude"), _g(row, "Bloom_Longitude")
            datum, landmark = _g(row, "Bloom_Datum"), _g(row, "Landmark")
            existing = self.conn.execute(
                "SELECT location_id FROM event WHERE bloom_report_id=%s", (eid,)).fetchone()
            vals = (cid, clean(_g(row, "Report_Type")), parse_date(_g(row, "Observation_Date")),
                    parse_datetime(_g(row, "Bloom_Date_Created")), clean(_g(row, "Bloom_Type")),
                    clean(_g(row, "Bloom_Size")), clean(_g(row, "Bloom_Location")),
                    clean(_g(row, "Bloom_Texture")), clean(_g(row, "Surface_Water_Condition")),
                    clean(_g(row, "Weather_Condition")), clean(_g(row, "Reported_Advisory_Types")),
                    parse_bool(_g(row, "Has_Pictures")))
            if existing is None:
                loc = self._location_id(wb, lat, lon, datum, landmark)
                self.conn.execute(
                    """INSERT INTO event (bloom_report_id, case_id, location_id, report_type,
                         observation_date, bloom_date_created, bloom_type, bloom_size, bloom_location,
                         bloom_texture, surface_water_condition, weather_condition,
                         reported_advisory_types, has_pictures)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (eid, vals[0], loc, *vals[1:]))
                self._bump("events", True)
            else:
                # Update the existing location in place (avoid orphaning/duplicating locations).
                loc = existing["location_id"]
                if loc is not None:
                    self._update_location(loc, wb, lat, lon, datum, landmark)
                else:
                    loc = self._location_id(wb, lat, lon, datum, landmark)
                self.conn.execute(
                    """UPDATE event SET case_id=%s, location_id=%s, report_type=%s, observation_date=%s,
                         bloom_date_created=%s, bloom_type=%s, bloom_size=%s, bloom_location=%s,
                         bloom_texture=%s, surface_water_condition=%s, weather_condition=%s,
                         reported_advisory_types=%s, has_pictures=%s WHERE bloom_report_id=%s""",
                    (vals[0], loc, *vals[1:], eid))
                self._bump("events", False)
            self._events.add(eid)

    def _update_location(self, loc_id, waterbody_id, lat, lon, datum, landmark) -> None:
        lat, lon = parse_float(lat), parse_float(lon)
        geom = (lon, lat) if (lat is not None and lon is not None and (lat != 0 or lon != 0)) else None
        self.conn.execute(
            """UPDATE location SET waterbody_id=%s,
                 geom = CASE WHEN %s::float8 IS NULL THEN geom
                             ELSE ST_SetSRID(ST_MakePoint(%s,%s),4326) END,
                 bloom_datum=%s, landmark=%s WHERE id=%s""",
            (waterbody_id, None if geom is None else geom[0],
             geom[0] if geom else None, geom[1] if geom else None,
             clean(datum), clean(landmark), loc_id))

    def load_responses(self) -> None:
        skipped, seen_adv = 0, set()
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
            vals = (eid, cid, clean(_g(row, "Response_Category")), clean(_g(row, "Response_Type")),
                    parse_datetime(_g(row, "Response_DateTimeStamp")))
            if self._exists("response", "response_action_id", rid):
                self.conn.execute(
                    """UPDATE response SET bloom_report_id=%s, case_id=%s, response_category=%s,
                         response_type=%s, response_datetimestamp=%s WHERE response_action_id=%s""",
                    (*vals, rid))
                self._bump("responses", False)
            else:
                self.conn.execute(
                    """INSERT INTO response (response_action_id, bloom_report_id, case_id,
                         response_category, response_type, response_datetimestamp)
                       VALUES (%s,%s,%s,%s,%s,%s)""", (rid, *vals))
                self._bump("responses", True)

            aid = parse_int(_g(row, "Advisory_ID"))
            if aid is not None and aid not in seen_adv:
                avals = (rid, clean(_g(row, "Advisory_Recommended")),
                         parse_date(_g(row, "Advisory_Start_Date")),
                         parse_date(_g(row, "Advisory_End_Date")), clean(_g(row, "Advisory_Detail")),
                         parse_float(_g(row, "Spatial_Extent_of_Advisory")),
                         clean(_g(row, "Extent_Unit_of_Measure")),
                         parse_bool(_g(row, "DisplayAdvisoryToMap")),
                         parse_date(_g(row, "Advisory_Date_of_Recommendation")),
                         parse_datetime(_g(row, "Advisory_Date")))
                if self._exists("advisory", "advisory_id", aid):
                    self.conn.execute(
                        """UPDATE advisory SET response_action_id=%s, advisory_recommended=%s,
                             advisory_start_date=%s, advisory_end_date=%s, advisory_detail=%s,
                             spatial_extent_of_advisory=%s, extent_unit_of_measure=%s,
                             display_advisory_on_map=%s, advisory_date_of_recommendation=%s,
                             advisory_date=%s WHERE advisory_id=%s""", (*avals, aid))
                    self._bump("advisories", False)
                else:
                    self.conn.execute(
                        """INSERT INTO advisory (advisory_id, response_action_id, advisory_recommended,
                             advisory_start_date, advisory_end_date, advisory_detail,
                             spatial_extent_of_advisory, extent_unit_of_measure,
                             display_advisory_on_map, advisory_date_of_recommendation, advisory_date)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", (aid, *avals))
                    self._bump("advisories", True)
                seen_adv.add(aid)
        self.report.skipped["responses"] = skipped

    def load_results(self) -> None:
        for row in _rows(self.data_dir / "results.csv"):
            res_uid = clean(_g(row, "RESULT ID UNIQUE", "RESULT_ID_UNIQUE"))
            if res_uid is None:
                continue
            eid = parse_int(_g(row, "Bloom_Report_ID"))
            eid = eid if eid in self._events else None
            cid = parse_int(_g(row, "Case_ID"))
            cid = cid if cid in self._cases else None
            analyte_id = self._analyte_id(_g(row, "Analysis_Type"), _g(row, "Analyte_Class"),
                                          _g(row, "Analyte"))
            mval = parse_float(_g(row, "Measurement_Value"))
            mtext = clean(_g(row, "Measurement_Value")) if mval is None else None
            rvals = (parse_int(_g(row, "Result_ID")), analyte_id,
                     parse_data_type(_g(row, "Data_Type")), clean(_g(row, "Measurement_Type")),
                     clean(_g(row, "Method")), mval, mtext, clean(_g(row, "Measurement_Unit")),
                     clean(_g(row, "Taxa")), parse_date(_g(row, "Results_Date")))
            if self._exists("result", "result_id_unique", res_uid):
                # Update the published result in place; keep its existing sample.
                self.conn.execute(
                    """UPDATE result SET result_id=%s, analyte_id=%s, data_type=%s, measurement_type=%s,
                         method=%s, measurement_value=%s, measurement_text=%s, measurement_unit=%s,
                         taxa=%s, results_date=%s WHERE result_id_unique=%s""", (*rvals, res_uid))
                self._bump("results", False)
            else:
                sid = self.conn.execute(
                    """INSERT INTO sample (bloom_report_id, case_id, sample_id, sample_type,
                         sample_location, site, sample_date) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (eid, cid, clean(_g(row, "Sample_ID")), clean(_g(row, "Sample_Type")),
                     clean(_g(row, "Sample_Location")), clean(_g(row, "Site")),
                     parse_date(_g(row, "Sample_Date")))).fetchone()["id"]
                self.conn.execute(
                    """INSERT INTO result (result_id_unique, result_id, sample_id, analyte_id, data_type,
                         measurement_type, method, measurement_value, measurement_text,
                         measurement_unit, taxa, results_date)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (res_uid, rvals[0], sid, rvals[1], *rvals[2:]))
                self._bump("results", True)


def _bump_sequences(conn) -> None:
    """Advance id sequences past the max published id so app-created rows don't collide."""
    for table, col in [("hab_case", "case_id"), ("event", "bloom_report_id"),
                       ("response", "response_action_id"), ("advisory", "advisory_id")]:
        conn.execute(
            f"""SELECT setval(pg_get_serial_sequence('{table}', '{col}'),
                    GREATEST((SELECT COALESCE(max({col}), 1) FROM {table}),
                             nextval(pg_get_serial_sequence('{table}', '{col}')) - 1))""")


def refresh_from_dir(conn: psycopg.Connection, data_dir, *, dry_run: bool = True) -> LoadReport:
    """Upsert the flat files in `data_dir` into the schema. dry_run rolls back after counting."""
    loader = RefreshLoader(conn, data_dir)
    try:
        loader.load_cases()
        loader.load_events()
        loader.load_responses()
        loader.load_results()
        if dry_run:
            conn.rollback()
        else:
            _bump_sequences(conn)
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    return loader.report


def refresh_from_ca_gov(conn: psycopg.Connection, *, dry_run: bool = True, workdir=None) -> LoadReport:
    """Fetch the published flat files from data.ca.gov, then upsert them (or dry-run preview)."""
    import tempfile
    tmp = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="fhab_refresh_"))
    fetch_published(tmp)
    return refresh_from_dir(conn, tmp, dry_run=dry_run)
