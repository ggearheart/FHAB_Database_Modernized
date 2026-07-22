"""Operational DB helpers: see what's running and clear stuck sessions.

When a long request (geo-boundary build, data.ca.gov refresh) is killed at the gunicorn
--timeout, Postgres may keep its connection as "idle in transaction" holding locks, which
wedges every later write. These read pg_stat_activity (still fast even while writes block)
and let an admin terminate the offenders from the UI instead of needing psql.

A role can terminate its own backends, so this works with the app's DATABASE_URL user.
"""

from __future__ import annotations

import psycopg


def session_activity(conn: psycopg.Connection) -> list[dict]:
    """Other backends on this database: state, how long idle/blocked, blocking PID, query head."""
    return conn.execute(
        """
        SELECT a.pid,
               a.usename,
               a.state,
               a.wait_event_type,
               a.wait_event,
               EXTRACT(epoch FROM now() - a.state_change)::int AS state_secs,
               EXTRACT(epoch FROM now() - a.xact_start)::int   AS xact_secs,
               pg_blocking_pids(a.pid)                          AS blocked_by,
               left(regexp_replace(a.query, '\\s+', ' ', 'g'), 90) AS query
        FROM pg_stat_activity a
        WHERE a.datname = current_database()
          AND a.pid <> pg_backend_pid()
          AND a.backend_type = 'client backend'
        ORDER BY a.state_change
        """
    ).fetchall()


def clear_stuck(conn: psycopg.Connection, older_than_secs: int = 60) -> list[int]:
    """Terminate idle-in-transaction backends older than `older_than_secs`. Returns killed PIDs.

    Targets only 'idle in transaction' (and its aborted variant) — never active queries — so it
    releases zombie locks without interrupting real work.
    """
    rows = conn.execute(
        """
        SELECT pid, pg_terminate_backend(pid) AS killed
        FROM pg_stat_activity
        WHERE datname = current_database()
          AND pid <> pg_backend_pid()
          AND state IN ('idle in transaction', 'idle in transaction (aborted)')
          AND state_change < now() - make_interval(secs => %s)
        """,
        (older_than_secs,),
    ).fetchall()
    conn.commit()
    return [r["pid"] for r in rows if r["killed"]]


def activity_summary(conn: psycopg.Connection) -> dict:
    """Counts for the admin page header: total client backends, active, idle-in-transaction, blocked."""
    rows = session_activity(conn)
    return {
        "total": len(rows),
        "active": sum(1 for r in rows if r["state"] == "active"),
        "idle_in_transaction": sum(1 for r in rows if (r["state"] or "").startswith("idle in transaction")),
        "blocked": sum(1 for r in rows if r["blocked_by"]),
    }
