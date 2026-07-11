"""Admin-managed application settings (key/value in app_setting).

Read on the privileged/owner connection (bypasses RLS) so system events like new-report
notifications can consult them; written by admins via the settings screen.
"""

from __future__ import annotations

import psycopg

# Setting keys.
EMAIL_NEW_REPORT = "notify_email_new_report"   # "1" = email/forward new-report notices
FORWARD_TO = "notify_forward_to"               # address to forward new-report notices to


def get_setting(conn: psycopg.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM app_setting WHERE key = %s", (key,)).fetchone()
    return row["value"] if row and row["value"] is not None else default


def set_setting(conn: psycopg.Connection, key: str, value: str | None, user_id: int | None = None) -> None:
    conn.execute(
        """INSERT INTO app_setting (key, value, updated_by, updated_at)
           VALUES (%s, %s, %s, now())
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value,
                                           updated_by = EXCLUDED.updated_by, updated_at = now()""",
        (key, value, user_id))
    conn.commit()


def email_new_report_enabled(conn) -> bool:
    return get_setting(conn, EMAIL_NEW_REPORT) == "1"
