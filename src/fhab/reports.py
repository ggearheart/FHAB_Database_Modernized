"""Enter a new bloom report as a given user, under Row-Level Security.

The core `enter_report` runs as the supplied app user (via fhab.auth.acting_as), so access
policies apply exactly as they would for that role — a staffer can file in their region, a
contributor files data owned by their org, and anyone without write permission is rejected.
"""

from __future__ import annotations

import uuid
from datetime import date

import psycopg

from .auth import acting_as


def enter_report(
    conn: psycopg.Connection,
    user_id: int,
    *,
    water_body_name: str,
    region: str | None = None,
    county: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    observation_date: date | None = None,
    report_type: str = "Staff entry",
    bloom_type: str | None = None,
    bloom_size: str | None = None,
    bloom_location: str | None = None,
    bloom_texture: str | None = None,
    description: str | None = None,
    owner_org: str | None = None,
    determination: str | None = None,
    bloom_report_id: int | None = None,
) -> int:
    """Create a report (waterbody + location + event) as `user_id`. Returns the report id.

    Raises psycopg.Error if access policies reject the write (e.g. wrong region / no perms).
    """
    # Allocate the next id with the privileged connection (sees all rows) before switching role.
    if bloom_report_id is None:
        bloom_report_id = conn.execute(
            "SELECT coalesce(max(bloom_report_id), 0) + 1 AS n FROM event"
        ).fetchone()["n"]

    # Use plain INSERT + currval rather than RETURNING: when a staffer files on behalf of
    # another region, RETURNING would read the new row back and trip the region-scoped read
    # policy. currval reads the sequence, which is not subject to RLS.
    with acting_as(conn, user_id):
        wb = conn.execute(
            "SELECT id FROM waterbody WHERE water_body_name = %s AND county IS NOT DISTINCT FROM %s",
            (water_body_name, county),
        ).fetchone()
        if wb:
            wb_id = wb["id"]
        else:
            conn.execute(
                """INSERT INTO waterbody (water_body_name, county, regional_water_board)
                   VALUES (%s, %s, %s)""",
                (water_body_name, county, region),
            )
            wb_id = conn.execute(
                "SELECT currval(pg_get_serial_sequence('waterbody', 'id')) AS id"
            ).fetchone()["id"]

        if lat is not None and lon is not None:
            conn.execute(
                """INSERT INTO location (waterbody_id, geom)
                   VALUES (%s, ST_SetSRID(ST_MakePoint(%s, %s), 4326))""",
                (wb_id, lon, lat),
            )
        else:
            conn.execute("INSERT INTO location (waterbody_id) VALUES (%s)", (wb_id,))
        loc_id = conn.execute(
            "SELECT currval(pg_get_serial_sequence('location', 'id')) AS id"
        ).fetchone()["id"]

        conn.execute(
            """INSERT INTO event
                 (bloom_report_id, location_id, report_type, observation_date, bloom_type,
                  bloom_size, bloom_location, bloom_texture, bloom_description, owner_org,
                  determination_code, event_status)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'suspected')""",
            (bloom_report_id, loc_id, report_type, observation_date or date.today(),
             bloom_type, bloom_size, bloom_location, bloom_texture, description, owner_org,
             determination),
        )
        conn.commit()

    return bloom_report_id


def update_report(conn: psycopg.Connection, user_id: int, bloom_report_id: int, *,
                  observation_date: str | date | None = None, bloom_type: str | None = None,
                  bloom_size: str | None = None, bloom_location: str | None = None,
                  bloom_texture: str | None = None, surface_water_condition: str | None = None,
                  weather_condition: str | None = None, bloom_description: str | None = None,
                  determination: str | None = None) -> None:
    """Edit a report's summary / field-verification info, as `user_id` (under RLS)."""
    with acting_as(conn, user_id):
        conn.execute(
            """UPDATE event SET
                 observation_date = %s, bloom_type = %s, bloom_size = %s, bloom_location = %s,
                 bloom_texture = %s, surface_water_condition = %s, weather_condition = %s,
                 bloom_description = %s, determination_code = %s
               WHERE bloom_report_id = %s""",
            (observation_date or None, bloom_type, bloom_size, bloom_location, bloom_texture,
             surface_water_condition, weather_condition, bloom_description, determination,
             bloom_report_id),
        )
        conn.commit()


def add_result(conn: psycopg.Connection, user_id: int, bloom_report_id: int, *,
               data_type: str, sample_date: str | date | None = None,
               analyte_id: int | None = None, measurement_value: float | None = None,
               measurement_unit: str | None = None, method: str | None = None,
               res_qual_code: str | None = None, taxa: str | None = None,
               collected_by: str | None = None, sample_label: str | None = None,
               site: str | None = None) -> str:
    """Record a sample + result (field verification or lab) on a report, as `user_id`.

    Returns the new result's unique id. Uses currval (not RETURNING) so it works even when
    a staffer files for another region.
    """
    ruid = uuid.uuid4().hex
    with acting_as(conn, user_id):
        conn.execute(
            """INSERT INTO sample (bloom_report_id, sample_date, sample_id, site, collected_by)
               VALUES (%s, %s, %s, %s, %s)""",
            (bloom_report_id, sample_date or None, sample_label, site, collected_by),
        )
        sample_id = conn.execute(
            "SELECT currval(pg_get_serial_sequence('sample', 'id')) AS id").fetchone()["id"]
        conn.execute(
            """INSERT INTO result
                 (result_id_unique, sample_id, analyte_id, data_type, method,
                  measurement_value, measurement_unit, res_qual_code, taxa, results_date)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (ruid, sample_id, analyte_id, data_type, method, measurement_value,
             measurement_unit, res_qual_code, taxa, sample_date or None),
        )
        conn.commit()
    return ruid
