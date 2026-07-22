"""Regenerate the published flat files from the normalized model (DIS-2a).

This emits the modeled columns with their published header names. Full-fidelity
reproduction of every published column (and the derived count/flag columns) is incremental
work as more source columns are loaded; the round-trip of the modeled core is validated
in the test suite. Veterinary results are excluded from the public results export.
"""

from __future__ import annotations

import csv
from pathlib import Path

import psycopg

# (published header, SQL expression) for each export, in column order.
BLOOM_REPORT_COLUMNS = [
    ("Bloom_Report_ID", "e.bloom_report_id"),
    ("Case_ID", "e.case_id"),
    ("Report_Type", "e.report_type"),
    ("Bloom_Date_Created", "e.bloom_date_created"),
    ("Water_Body_Name", "w.water_body_name"),
    ("County", "w.county"),
    ("Regional_Water_Board", "w.regional_water_board"),
    ("Bloom_Latitude", "ST_Y(l.geom)"),
    ("Bloom_Longitude", "ST_X(l.geom)"),
    ("Observation_Date", "e.observation_date"),
    ("Has_Pictures", "e.has_pictures"),
    ("Bloom_Size", "e.bloom_size"),
    ("Bloom_Location", "e.bloom_location"),
    ("Bloom_Texture", "e.bloom_texture"),
    ("Water_Body_Type", "w.water_body_type"),
]

CASE_COLUMNS = [
    ("Case_ID", "c.case_id"),
    ("Case_Start_Date", "c.case_start_date"),
    ("Case_Year", "c.case_year"),
    ("Case_Water_Body_Name", "c.case_water_body_name"),
    ("Case_Class", "c.case_class"),
    ("Case_Status", "c.case_status"),
    ("Case_Lead", "c.case_lead"),
    ("Case_End_Date", "c.case_end_date"),
]

RESPONSE_COLUMNS = [
    ("Response_Action_ID", "r.response_action_id"),
    ("Response_Category", "r.response_category"),
    ("Bloom_Report_ID", "r.bloom_report_id"),
    ("Case_ID", "r.case_id"),
    ("Response_Type", "r.response_type"),
    ("Advisory_ID", "a.advisory_id"),
    ("Advisory_Recommended", "a.advisory_recommended"),
    ("Advisory_Start_Date", "a.advisory_start_date"),
    ("Advisory_End_Date", "a.advisory_end_date"),
    ("DisplayAdvisoryToMap", "a.display_advisory_on_map"),
]

RESULT_COLUMNS = [
    ("RESULT ID UNIQUE", "r.result_id_unique"),
    ("Result_ID", "r.result_id"),
    ("Bloom_Report_ID", "s.bloom_report_id"),
    ("Case_ID", "s.case_id"),
    ("Sample_Date", "s.sample_date"),
    ("Sample_Type", "s.sample_type"),
    ("Data_Type", "r.data_type::text"),
    ("Analysis_Type", "an.analysis_type"),
    ("Analyte_Class", "an.analyte_class"),
    ("Analyte", "an.analyte"),
    ("Method", "r.method"),
    ("Measurement_Value", "coalesce(r.measurement_value::text, r.measurement_text)"),
    ("Measurement_Unit", "r.measurement_unit"),
    ("Taxa", "r.taxa"),
]

# CEDEN Surface Water Chemistry Results structure (https://data.ca.gov/dataset/surface-water-chemistry-results).
# ResultRowID (our unique result id) is the join key to the crosswalk below.
CHEMISTRY_COLUMNS = [
    ("ResultRowID", "r.result_id_unique"),
    ("StationCode", "st.station_code"),
    ("StationName", "st.station_name"),
    ("SampleDate", "s.sample_date"),
    ("CollectionTime", "s.sample_time"),
    ("LocationCode", "s.sample_location"),
    ("SampleTypeCode", "s.sample_type"),
    ("MatrixName", "coalesce(r.matrix_name, 'samplewater')"),
    ("MethodName", "r.method"),
    ("AnalyteName", "an.analyte"),
    ("FractionName", "r.fraction_name"),
    ("UnitName", "coalesce(r.measurement_unit, an.default_unit)"),
    ("Result", "coalesce(r.measurement_value::text, r.measurement_text)"),
    ("ResQualCode", "r.res_qual_code"),
    ("MDL", "r.mdl"),
    ("RL", "r.rl"),
    ("QACode", "r.qa_code"),
    ("ComplianceCode", "r.compliance_code"),
    ("ProjectCode", "s.project_code"),
    ("AgencyCode", "s.lab_agency_code"),
    ("LabSampleID", "s.lab_sample_id"),
    ("LabBatch", "s.lab_batch"),
    ("TargetLatitude", "ST_Y(st.geom)"),
    ("TargetLongitude", "ST_X(st.geom)"),
    ("Datum", "coalesce(st.datum, CASE WHEN st.geom IS NOT NULL THEN 'WGS84' END)"),
]

# Crosswalk: each chemistry result -> geospatial backbone + FHAB report/case (where they exist).
CROSSWALK_COLUMNS = [
    ("ResultRowID", "r.result_id_unique"),
    ("StationCode", "st.station_code"),
    ("SampleDate", "s.sample_date"),
    ("Sample_ID", "s.id"),                    # stable sample key — groups a sample's analyte rows
    ("Sampling_Event_ID", "s.lab_batch_id"),  # the ingest batch (sampling event), where applicable
    ("AnalyteName", "an.analyte"),
    ("Bloom_Report_ID", "s.bloom_report_id"),
    ("Case_ID", "coalesce(s.case_id, e.case_id)"),
    # Authoritative fill: prefer the linked report's waterbody, else the value derived from the
    # station's coordinates by point-in-polygon against the authoritative boundary layers
    # (WBD HUC12 subwatershed name, CA counties, Regional Water Board boundaries). See fhab.geo.
    ("Water_Body_Name", "coalesce(w.water_body_name, hu.name)"),
    ("Regional_Water_Board", "coalesce(w.regional_water_board, st.regional_water_board)"),
    ("County", "coalesce(w.county, st.county)"),
    ("HUC12", "coalesce(st.huc12, l.huc12)"),
    ("Latitude", "coalesce(ST_Y(st.geom), ST_Y(l.geom))"),
    ("Longitude", "coalesce(ST_X(st.geom), ST_X(l.geom))"),
    ("Station_GeoConnex", "st.geoconnex_uri"),
    ("Event_GeoConnex", "e.geoconnex_uri"),
]

# Shared join for the chemistry + crosswalk exports (one row per non-veterinary result).
_CHEM_FROM = """
    FROM result r
    JOIN sample s ON s.id = r.sample_id
    LEFT JOIN station st ON st.id = s.station_id
    LEFT JOIN analyte an ON an.id = r.analyte_id
    LEFT JOIN event e ON e.bloom_report_id = s.bloom_report_id
    LEFT JOIN location l ON l.id = e.location_id
    LEFT JOIN waterbody w ON w.id = l.waterbody_id
    LEFT JOIN huc12 hu ON hu.huc12 = coalesce(st.huc12, l.huc12)
    WHERE r.data_type IS DISTINCT FROM 'Veterinary'
    ORDER BY r.result_id_unique
"""

_QUERIES = {
    "bloom-report.csv": ("""
        SELECT {cols} FROM event e
        LEFT JOIN location l ON l.id = e.location_id
        LEFT JOIN waterbody w ON w.id = l.waterbody_id
        ORDER BY e.bloom_report_id
    """, BLOOM_REPORT_COLUMNS),
    "hab-cases.csv": ("""
        SELECT {cols} FROM hab_case c ORDER BY c.case_id
    """, CASE_COLUMNS),
    "hab-responses.csv": ("""
        SELECT {cols} FROM response r
        LEFT JOIN advisory a ON a.response_action_id = r.response_action_id
        ORDER BY r.response_action_id
    """, RESPONSE_COLUMNS),
    # Veterinary excluded from the public export (data dictionary requirement).
    "hab-results.csv": ("""
        SELECT {cols} FROM result r
        JOIN sample s ON s.id = r.sample_id
        LEFT JOIN analyte an ON an.id = r.analyte_id
        WHERE r.data_type IS DISTINCT FROM 'Veterinary'
        ORDER BY r.result_id_unique
    """, RESULT_COLUMNS),
    "chemistry-results.csv": ("SELECT {cols} " + _CHEM_FROM, CHEMISTRY_COLUMNS),
    "chemistry-crosswalk.csv": ("SELECT {cols} " + _CHEM_FROM, CROSSWALK_COLUMNS),
}


# Human-facing metadata for each dataset (slug -> title, description), keyed without ".csv".
DATASETS = {
    "bloom-report": ("FHAB Bloom Reports",
                     "Reported and observed freshwater HAB events (one row per report)."),
    "hab-cases": ("FHAB Cases", "Case records that group related reports for a waterbody/year."),
    "hab-responses": ("FHAB Responses", "Response actions and posted advisories."),
    "hab-results": ("FHAB Results", "Field and laboratory results (veterinary excluded)."),
    "chemistry-results": ("CEDEN Chemistry Results",
                          "Analyte results in the CEDEN Surface Water Chemistry structure "
                          "(veterinary excluded). Joins to the crosswalk on ResultRowID."),
    "chemistry-crosswalk": ("FHAB ↔ CEDEN Crosswalk",
                            "Links each chemistry result to its sample and sampling event, the "
                            "geospatial backbone (HUC-12, lat/lon, GeoConnex), and FHAB report/case "
                            "IDs where they exist. Joins to CEDEN Chemistry Results on ResultRowID."),
}


def _filename(slug: str) -> str:
    return slug if slug.endswith(".csv") else slug + ".csv"


def fetch_flatfile(conn: psycopg.Connection, name: str):
    """Run one published export; return (headers, records) where records are JSON-friendly dicts.

    Single source of truth for both the CSV writer and the open-data JSON API, so both expose
    exactly the published column set (no reporter PII / illness; veterinary excluded).
    """
    sql_tmpl, columns = _QUERIES[_filename(name)]
    select = ", ".join(f"{expr} AS \"{hdr}\"" for hdr, expr in columns)
    rows = conn.execute(sql_tmpl.format(cols=select)).fetchall()
    headers = [hdr for hdr, _ in columns]
    records = [{h: row[h] for h in headers} for row in rows]
    return headers, records


def export_flatfile(conn: psycopg.Connection, name: str, out_dir: Path) -> int:
    """Write one published flat file; return the row count."""
    headers, records = fetch_flatfile(conn, name)
    out = Path(out_dir) / _filename(name)
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(headers)
        for rec in records:
            writer.writerow(["" if rec[h] is None else rec[h] for h in headers])
    return len(records)


def export_all(conn: psycopg.Connection, out_dir: Path) -> dict[str, int]:
    """Regenerate all four published flat files into out_dir."""
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    return {name: export_flatfile(conn, name, out_dir) for name in _QUERIES}
