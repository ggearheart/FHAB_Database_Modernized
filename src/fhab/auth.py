"""Application-side helpers for users, role grants, and acting as a user under RLS.

The privileged (owner) connection used by loaders bypasses Row-Level Security. To exercise
access control the way the application will, use `acting_as(conn, user_id)`: it switches the
connection to the non-owning `fhab_app` role and sets the `fhab.user_id` session variable, so
RLS policies apply. See docs/USER_ROLES.md and sql/access_control.sql.
"""

from __future__ import annotations

from contextlib import contextmanager

import psycopg


def create_user(conn: psycopg.Connection, email: str, full_name: str | None = None,
                personnel_code: str | None = None) -> int:
    """Create (or fetch) an application user; returns the user id."""
    row = conn.execute(
        """INSERT INTO app_user (email, full_name, personnel_code)
           VALUES (%s, %s, %s)
           ON CONFLICT (email) DO UPDATE SET full_name = COALESCE(EXCLUDED.full_name, app_user.full_name)
           RETURNING id""",
        (email, full_name, personnel_code),
    ).fetchone()
    conn.commit()
    return row["id"]


def grant_role(conn: psycopg.Connection, user_id: int, role_code: str, *,
               region: str | None = None, ddw_district: str | None = None,
               org: str | None = None, waterbody_id: int | None = None) -> None:
    """Grant a role to a user within an optional scope."""
    conn.execute(
        """INSERT INTO user_role
             (user_id, role_code, scope_region, scope_ddw_district, scope_org, scope_waterbody_id)
           VALUES (%s,%s,%s,%s,%s,%s)
           ON CONFLICT DO NOTHING""",
        (user_id, role_code, region, ddw_district, org, waterbody_id),
    )
    conn.commit()


@contextmanager
def acting_as(conn: psycopg.Connection, user_id: int | None):
    """Run queries as `user_id` under RLS (via the fhab_app role). Resets on exit.

    Pass user_id=None to act as an anonymous public visitor.
    """
    conn.execute("SET ROLE fhab_app")
    conn.execute("SELECT set_config('fhab.user_id', %s, false)",
                 ("" if user_id is None else str(user_id),))
    try:
        yield conn
    finally:
        conn.execute("RESET ROLE")
        conn.execute("SELECT set_config('fhab.user_id', '', false)")
