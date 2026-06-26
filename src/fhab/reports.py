"""Enter a new bloom report as a given user, under Row-Level Security.

The core `enter_report` runs as the supplied app user (via fhab.auth.acting_as), so access
policies apply exactly as they would for that role — a staffer can file in their region, a
contributor files data owned by their org, and anyone without write permission is rejected.
"""

from __future__ import annotations

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
                  event_status)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'suspected')""",
            (bloom_report_id, loc_id, report_type, observation_date or date.today(),
             bloom_type, bloom_size, bloom_location, bloom_texture, description, owner_org),
        )
        conn.commit()

    return bloom_report_id
