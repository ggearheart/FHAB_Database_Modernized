"""Case-management helpers (run under Row-Level Security as the acting user).

A case is a staff folder grouping one or more reports for a waterbody. Per the case-management
manual: a case cannot span Regional Boards, is one waterbody (or waterbody+county), and covers a
single calendar year. The hab_case RLS write policy already restricts creation/edits to staff in
the case's region.
"""

from __future__ import annotations

from datetime import date

import psycopg

from .auth import acting_as

CASE_STATUSES = ["Open", "Ongoing", "Closed", "Re-opened"]


def create_case(conn: psycopg.Connection, user_id: int, *, water_body_name: str,
                region: str | None = None, county: str | None = None, year: int | None = None,
                case_class: str | None = None, case_lead: str | None = None,
                status: str = "Open") -> int:
    """Create a case for a waterbody (creating the waterbody if needed). Returns the case id."""
    # Reserved app-id range (>= 1e9) so app cases never collide with published case ids.
    case_id = conn.execute("SELECT nextval('app_case_id_seq') AS n").fetchone()["n"]
    with acting_as(conn, user_id):
        wb = conn.execute(
            "SELECT id FROM waterbody WHERE water_body_name = %s AND county IS NOT DISTINCT FROM %s",
            (water_body_name, county)).fetchone()
        if wb:
            wb_id = wb["id"]
        else:
            conn.execute(
                "INSERT INTO waterbody (water_body_name, county, regional_water_board) VALUES (%s,%s,%s)",
                (water_body_name, county, region))
            wb_id = conn.execute(
                "SELECT currval(pg_get_serial_sequence('waterbody', 'id')) AS id").fetchone()["id"]
        conn.execute(
            """INSERT INTO hab_case
                 (case_id, waterbody_id, case_water_body_name, case_class, case_status,
                  case_lead, case_year, case_start_date)
               VALUES (%s,%s,%s,%s,%s,%s,%s, current_date)""",
            (case_id, wb_id, water_body_name, case_class, status, case_lead,
             year or date.today().year))
        conn.commit()
    return case_id


def update_case(conn: psycopg.Connection, user_id: int, case_id: int, *,
                status: str | None = None, case_lead: str | None = None,
                case_class: str | None = None, year: int | None = None) -> None:
    """Edit a case's status / lead / class / year (staff, in the case's region)."""
    with acting_as(conn, user_id):
        conn.execute(
            """UPDATE hab_case SET case_status = %s, case_lead = %s, case_class = %s,
                   case_year = %s, case_end_date = CASE WHEN %s = 'Closed' THEN current_date ELSE case_end_date END
               WHERE case_id = %s""",
            (status, case_lead, case_class, year, status, case_id))
        conn.commit()


def assign_report_to_case(conn: psycopg.Connection, user_id: int, bloom_report_id: int,
                          case_id: int | None) -> None:
    """Assign (or, with case_id=None, unassign) a report to a case."""
    with acting_as(conn, user_id):
        conn.execute("UPDATE event SET case_id = %s WHERE bloom_report_id = %s",
                     (case_id, bloom_report_id))
        conn.commit()
