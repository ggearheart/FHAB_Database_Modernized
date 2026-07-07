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
    landmark: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    observation_date: date | None = None,
    report_type: str = "Staff entry",
    bloom_type: str | None = None,
    bloom_size: str | None = None,
    bloom_location: str | None = None,
    bloom_texture: str | None = None,
    bloom_textures: list | None = None,
    surface_water_condition: str | None = None,
    weather_condition: str | None = None,
    signs_posted: str | None = None,
    has_pictures: bool | None = None,
    description: str | None = None,
    management_comments: str | None = None,
    reporter_name: str | None = None,
    reporter_email: str | None = None,
    reporter_phone: str | None = None,
    reporter_org: str | None = None,
    owner_org: str | None = None,
    determination: str | None = None,
    bloom_report_id: int | None = None,
) -> int:
    """Create a report (waterbody + location + event) as `user_id`. Returns the report id.

    Raises psycopg.Error if access policies reject the write (e.g. wrong region / no perms).
    """
    # Allocate the id from the reserved app-id sequence (>= 1e9, never overlaps published ids),
    # on the privileged connection before switching role. See docs/GOVERNANCE_REVIEW.md #2.
    if bloom_report_id is None:
        bloom_report_id = conn.execute(
            "SELECT nextval('app_event_id_seq') AS n").fetchone()["n"]

    # Resolve the canonical waterbody with the privileged connection (before switching role):
    # waterbody_read is region-scoped, so a staffer filing cross-region — or matching a waterbody
    # with no region yet — could not otherwise see the existing row and would create a duplicate.
    # Dedup is a global concern; the event's own visibility stays region-policed below.
    wb = conn.execute(
        """SELECT id FROM waterbody
           WHERE lower(water_body_name) = lower(%s) AND county IS NOT DISTINCT FROM %s
           ORDER BY id LIMIT 1""",
        (water_body_name, county),
    ).fetchone()

    # Use plain INSERT + currval rather than RETURNING: when a staffer files on behalf of
    # another region, RETURNING would read the new row back and trip the region-scoped read
    # policy. currval reads the sequence, which is not subject to RLS.
    with acting_as(conn, user_id):
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
                """INSERT INTO location (waterbody_id, landmark, geom)
                   VALUES (%s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326))""",
                (wb_id, landmark, lon, lat),
            )
        else:
            conn.execute("INSERT INTO location (waterbody_id, landmark) VALUES (%s, %s)",
                         (wb_id, landmark))
        loc_id = conn.execute(
            "SELECT currval(pg_get_serial_sequence('location', 'id')) AS id"
        ).fetchone()["id"]

        conn.execute(
            """INSERT INTO event
                 (bloom_report_id, location_id, report_type, observation_date, bloom_type,
                  bloom_size, bloom_location, bloom_texture, bloom_textures,
                  surface_water_condition, weather_condition, signs_posted, has_pictures,
                  bloom_description, management_comments, reporter_name, reporter_email,
                  reporter_phone, reporter_org, owner_org, determination_code, event_status)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'suspected')""",
            (bloom_report_id, loc_id, report_type, observation_date or date.today(),
             bloom_type, bloom_size, bloom_location, bloom_texture, bloom_textures or None,
             surface_water_condition, weather_condition, signs_posted, has_pictures,
             description, management_comments, reporter_name, reporter_email, reporter_phone,
             reporter_org, owner_org, determination),
        )
        conn.commit()

    return bloom_report_id


def update_report(conn: psycopg.Connection, user_id: int, bloom_report_id: int, *,
                  observation_date: str | date | None = None, bloom_type: str | None = None,
                  bloom_size: str | None = None, bloom_location: str | None = None,
                  bloom_texture: str | None = None, bloom_textures: list | None = None,
                  surface_water_condition: str | None = None,
                  weather_condition: str | None = None, signs_posted: str | None = None,
                  has_pictures: bool | None = None, bloom_description: str | None = None,
                  management_comments: str | None = None,
                  determination: str | None = None) -> None:
    """Edit a report's summary / field-verification info, as `user_id` (under RLS)."""
    with acting_as(conn, user_id):
        conn.execute(
            """UPDATE event SET
                 observation_date = %s, bloom_type = %s, bloom_size = %s, bloom_location = %s,
                 bloom_texture = %s, bloom_textures = %s, surface_water_condition = %s,
                 weather_condition = %s, signs_posted = %s, has_pictures = %s,
                 bloom_description = %s, management_comments = %s, determination_code = %s
               WHERE bloom_report_id = %s""",
            (observation_date or None, bloom_type, bloom_size, bloom_location, bloom_texture,
             bloom_textures or None, surface_water_condition, weather_condition, signs_posted,
             has_pictures, bloom_description, management_comments, determination,
             bloom_report_id),
        )
        conn.commit()


# Subjects on the official suspected-illness/death matrix (order preserved for the form).
ILLNESS_SUBJECTS = ["Human", "Dog", "Pet", "Fish", "Wildlife", "Cattle", "Goat", "Horse",
                    "Sheep", "Livestock"]


def set_report_illness(conn: psycopg.Connection, user_id: int, bloom_report_id: int, *,
                       rows: list | None = None, none_observed: bool = False,
                       description: str | None = None) -> None:
    """Replace the suspected illness/death rows for a report (sensitive; staff-only via RLS).

    `rows` is an iterable of dicts: {subject, illness, death}. Only rows with illness or death
    set are stored. Also records the 'none observed' flag and free-text illness details.
    """
    with acting_as(conn, user_id):
        conn.execute("DELETE FROM report_illness WHERE bloom_report_id = %s", (bloom_report_id,))
        for r in rows or []:
            if r.get("illness") or r.get("death"):
                conn.execute(
                    """INSERT INTO report_illness (bloom_report_id, subject, illness, death)
                       VALUES (%s,%s,%s,%s)""",
                    (bloom_report_id, r["subject"], bool(r.get("illness")), bool(r.get("death"))),
                )
        conn.execute(
            "UPDATE event SET no_illness_observed = %s, illness_description = %s WHERE bloom_report_id = %s",
            (none_observed, description, bloom_report_id),
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


def add_response(conn: psycopg.Connection, user_id: int, bloom_report_id: int, *,
                 response_category: str = "Advisory", response_type: str | None = None,
                 updated_by: str | None = None, advisory_recommended: str | None = None,
                 advisory_detail: str | None = None, advisory_start_date: str | date | None = None,
                 advisory_end_date: str | date | None = None,
                 display_advisory_on_map: bool = False) -> int:
    """Record a response on a report and, if an advisory is recommended, post the advisory.

    Returns the new response id. Only staff may do this (enforced by RLS), so contributors
    cannot self-post advisories. An advisory is created only when `advisory_recommended` is set.
    """
    # Allocate ids from the reserved app-id sequences (>= 1e9) on the privileged connection.
    rid = conn.execute("SELECT nextval('app_response_id_seq') AS n").fetchone()["n"]
    case_row = conn.execute(
        "SELECT case_id FROM event WHERE bloom_report_id = %s", (bloom_report_id,)).fetchone()
    case_id = case_row["case_id"] if case_row else None
    aid = None
    if advisory_recommended:
        aid = conn.execute("SELECT nextval('app_advisory_id_seq') AS n").fetchone()["n"]

    with acting_as(conn, user_id):
        conn.execute(
            """INSERT INTO response
                 (response_action_id, bloom_report_id, case_id, response_category,
                  response_type, response_update_by, response_datetimestamp)
               VALUES (%s,%s,%s,%s,%s,%s, now())""",
            (rid, bloom_report_id, case_id, response_category, response_type, updated_by),
        )
        if advisory_recommended:
            conn.execute(
                """INSERT INTO advisory
                     (advisory_id, response_action_id, advisory_recommended, advisory_detail,
                      advisory_start_date, advisory_end_date, display_advisory_on_map,
                      advisory_date_of_recommendation, advisory_date)
                   VALUES (%s,%s,%s,%s,%s,%s,%s, current_date, now())""",
                (aid, rid, advisory_recommended, advisory_detail, advisory_start_date or None,
                 advisory_end_date or None, display_advisory_on_map),
            )
        conn.commit()
    return rid
