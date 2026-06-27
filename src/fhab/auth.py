"""Application-side helpers for users, role grants, and acting as a user under RLS.

The privileged (owner) connection used by loaders bypasses Row-Level Security. To exercise
access control the way the application will, use `acting_as(conn, user_id)`: it switches the
connection to the non-owning `fhab_app` role and sets the `fhab.user_id` session variable, so
RLS policies apply. See docs/USER_ROLES.md and sql/access_control.sql.
"""

from __future__ import annotations

from contextlib import contextmanager

import psycopg
from werkzeug.security import check_password_hash, generate_password_hash


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


def set_password(conn: psycopg.Connection, user_id: int, password: str) -> None:
    """Set a user's password (hashed)."""
    conn.execute("UPDATE app_user SET password_hash = %s WHERE id = %s",
                 (generate_password_hash(password), user_id))
    conn.commit()


def authenticate(conn: psycopg.Connection, email: str, password: str) -> dict | None:
    """Return the user row if email/password match and the account is active, else None."""
    row = conn.execute(
        "SELECT id, email, full_name, password_hash, is_active FROM app_user WHERE email = %s",
        (email,),
    ).fetchone()
    if not row or not row["is_active"] or not row["password_hash"]:
        return None
    if not check_password_hash(row["password_hash"], password):
        return None
    return row


def list_roles_for(conn: psycopg.Connection, user_id: int) -> list[str]:
    """Role codes held by a user."""
    return [r["role_code"] for r in conn.execute(
        "SELECT role_code FROM user_role WHERE user_id = %s", (user_id,)).fetchall()]


def revoke_role(conn: psycopg.Connection, user_id: int, role_code: str) -> None:
    conn.execute("DELETE FROM user_role WHERE user_id = %s AND role_code = %s", (user_id, role_code))
    conn.commit()


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


def user_regions(conn: psycopg.Connection, user_id: int) -> list[str]:
    """Return the regions a user is scoped to (empty = unscoped / admin / contributor)."""
    rows = conn.execute(
        "SELECT DISTINCT scope_region FROM user_role WHERE user_id = %s AND scope_region IS NOT NULL",
        (user_id,),
    ).fetchall()
    return [r["scope_region"] for r in rows]


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
        # Best-effort cleanup. Must never raise or it would mask the original error and (on a
        # busy/aborted connection) crash the request. Roll back any in-flight/failed transaction
        # first — a no-op after a successful read or an explicit commit — then reset session state.
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        for stmt in ("RESET ROLE", "SELECT set_config('fhab.user_id', '', false)"):
            try:
                conn.execute(stmt)
            except Exception:  # noqa: BLE001
                pass
