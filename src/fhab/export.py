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
    ("Case_Status", "initcap(c.case_status::text)"),
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
    ("Advisory_Recommended", "initcap(a.advisory_recommended::text)"),
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
}


def export_flatfile(conn: psycopg.Connection, name: str, out_dir: Path) -> int:
    """Write one published flat file; return the row count."""
    sql_tmpl, columns = _QUERIES[name]
    select = ", ".join(f"{expr} AS \"{hdr}\"" for hdr, expr in columns)
    rows = conn.execute(sql_tmpl.format(cols=select)).fetchall()
    out = Path(out_dir) / name
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([hdr for hdr, _ in columns])
        for row in rows:
            writer.writerow(["" if row[h] is None else row[h] for h, _ in columns])
    return len(rows)


def export_all(conn: psycopg.Connection, out_dir: Path) -> dict[str, int]:
    """Regenerate all four published flat files into out_dir."""
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    return {name: export_flatfile(conn, name, out_dir) for name in _QUERIES}
