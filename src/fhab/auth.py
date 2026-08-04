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


def request_signup(conn: psycopg.Connection, email: str, full_name: str | None,
                   password: str, note: str | None = None) -> int | None:
    """Self-service account request: creates an inactive, pending account with a password.

    Returns the new user id, or None if an account with that email already exists (active or
    pending). The account cannot sign in until an admin approves it.
    """
    email = (email or "").strip().lower()
    row = conn.execute(
        """INSERT INTO app_user (email, full_name, password_hash, is_active, signup_pending, signup_note)
           VALUES (%s, %s, %s, false, true, %s)
           ON CONFLICT (email) DO NOTHING
           RETURNING id""",
        (email, (full_name or "").strip() or None, generate_password_hash(password),
         (note or "").strip() or None),
    ).fetchone()
    conn.commit()
    return row["id"] if row else None


def is_pending_signup(conn: psycopg.Connection, email: str) -> bool:
    """True if the email belongs to an account awaiting admin approval."""
    r = conn.execute("SELECT signup_pending FROM app_user WHERE email = %s AND signup_pending",
                     ((email or "").strip().lower(),)).fetchone()
    return bool(r)


def approve_signup(conn: psycopg.Connection, user_id: int) -> None:
    """Activate a pending account (an admin grants roles separately)."""
    conn.execute("UPDATE app_user SET is_active = true, signup_pending = false WHERE id = %s",
                 (user_id,))
    conn.commit()


def reject_signup(conn: psycopg.Connection, user_id: int) -> None:
    """Delete a pending signup that was declined."""
    conn.execute("DELETE FROM app_user WHERE id = %s AND signup_pending", (user_id,))
    conn.commit()


def authenticate(conn: psycopg.Connection, email: str, password: str) -> dict | None:
    """Return the user row if email/password match and the account is active, else None.

    Records the sign-in time and surfaces must_change_password so the login flow can force a
    reset after an admin sets a temporary password.
    """
    row = conn.execute(
        "SELECT id, email, full_name, password_hash, is_active, must_change_password "
        "FROM app_user WHERE email = %s",
        ((email or "").strip().lower(),),
    ).fetchone()
    if not row or not row["is_active"] or not row["password_hash"]:
        return None
    if not check_password_hash(row["password_hash"], password):
        return None
    conn.execute("UPDATE app_user SET last_login_at = now() WHERE id = %s", (row["id"],))
    conn.commit()
    return row


# ---------- account management (admin) ----------

def gen_password(length: int = 12) -> str:
    """A readable temporary password (no ambiguous characters)."""
    import secrets
    alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def reset_password(conn: psycopg.Connection, user_id: int, password: str,
                   require_change: bool = True) -> None:
    """Admin sets a (temporary) password; by default the user must change it at next sign-in."""
    conn.execute("UPDATE app_user SET password_hash = %s, must_change_password = %s WHERE id = %s",
                 (generate_password_hash(password), require_change, user_id))
    conn.commit()


def change_own_password(conn: psycopg.Connection, user_id: int, current: str, new: str) -> bool:
    """A user changes their own password. Verifies the current one; clears must_change. Returns
    False if the current password is wrong."""
    row = conn.execute("SELECT password_hash FROM app_user WHERE id = %s", (user_id,)).fetchone()
    if not row or not row["password_hash"] or not check_password_hash(row["password_hash"], current):
        return False
    conn.execute("UPDATE app_user SET password_hash = %s, must_change_password = false WHERE id = %s",
                 (generate_password_hash(new), user_id))
    conn.commit()
    return True


def set_active(conn: psycopg.Connection, user_id: int, active: bool) -> None:
    """Deactivate (disable sign-in) or reactivate an account."""
    conn.execute("UPDATE app_user SET is_active = %s WHERE id = %s", (active, user_id))
    conn.commit()


def update_user(conn: psycopg.Connection, user_id: int, *, email: str | None = None,
                full_name: str | None = None) -> None:
    """Edit an account's email and/or display name. Raises ValueError on a duplicate email."""
    sets, p = [], []
    if email is not None:
        e = email.strip().lower()
        if conn.execute("SELECT 1 FROM app_user WHERE lower(email) = %s AND id <> %s",
                        (e, user_id)).fetchone():
            raise ValueError("Another account already uses that email.")
        sets.append("email = %s"); p.append(e)
    if full_name is not None:
        sets.append("full_name = %s"); p.append(full_name.strip() or None)
    if sets:
        conn.execute(f"UPDATE app_user SET {', '.join(sets)} WHERE id = %s", (*p, user_id))
        conn.commit()


def user_references(conn: psycopg.Connection, user_id: int) -> int:
    """How many work records reference this user (assignments, audit, ingests, activity). A user
    with references should be deactivated, not deleted, to preserve provenance."""
    return conn.execute(
        """SELECT (SELECT count(*) FROM sample WHERE assigned_to = %(u)s)
                + (SELECT count(*) FROM audit_log WHERE actor_id = %(u)s)
                + (SELECT count(*) FROM lab_batch WHERE uploaded_by = %(u)s)
                + (SELECT count(*) FROM user_role WHERE granted_by = %(u)s) AS n""",
        {"u": user_id}).fetchone()["n"]


def delete_user(conn: psycopg.Connection, user_id: int) -> None:
    """Hard-delete an account (and its role grants). Only for accounts with no work references —
    callers should check user_references() first and deactivate instead when it is non-zero."""
    conn.execute("DELETE FROM user_role WHERE user_id = %s", (user_id,))
    conn.execute("DELETE FROM app_user WHERE id = %s", (user_id,))
    conn.commit()


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
