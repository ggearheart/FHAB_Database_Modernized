"""Read the row-level audit log (governance #5). Writes happen in the DB via triggers
(sql/audit.sql); this is the query side for the admin browser.
"""

from __future__ import annotations

import psycopg

AUDITED_TABLES = ("event", "hab_case", "response", "advisory", "sample")


def recent(conn: psycopg.Connection, *, table: str | None = None, row_key: str | None = None,
           actor_id: int | None = None, action: str | None = None,
           limit: int = 100, offset: int = 0) -> list[dict]:
    """Recent audit entries, newest first, with the actor's email joined in. Each row carries a
    `diff` list of {col, before, after} for the changed columns."""
    cond, p = ["TRUE"], {"lim": limit, "off": offset}
    if table:
        cond.append("a.table_name = %(t)s"); p["t"] = table
    if row_key:
        cond.append("a.row_key = %(r)s"); p["r"] = row_key
    if actor_id:
        cond.append("a.actor_id = %(u)s"); p["u"] = actor_id
    if action in ("UPDATE", "DELETE"):
        cond.append("a.action = %(a)s"); p["a"] = action
    rows = conn.execute(
        f"""SELECT a.id, a.at, a.actor_id, a.table_name, a.row_key, a.action, a.changed,
                   a.before, a.after, u.email AS actor_email
            FROM audit_log a
            LEFT JOIN app_user u ON u.id = a.actor_id
            WHERE {' AND '.join(cond)}
            ORDER BY a.at DESC, a.id DESC
            LIMIT %(lim)s OFFSET %(off)s""", p).fetchall()
    for r in rows:
        before, after = r["before"] or {}, r["after"] or {}
        cols = r["changed"] if r["changed"] is not None else sorted(before)
        r["diff"] = [{"col": c, "before": before.get(c), "after": after.get(c)} for c in cols]
    return rows


def count(conn: psycopg.Connection, *, table=None, row_key=None, actor_id=None, action=None) -> int:
    cond, p = ["TRUE"], {}
    if table:
        cond.append("table_name = %(t)s"); p["t"] = table
    if row_key:
        cond.append("row_key = %(r)s"); p["r"] = row_key
    if actor_id:
        cond.append("actor_id = %(u)s"); p["u"] = actor_id
    if action in ("UPDATE", "DELETE"):
        cond.append("action = %(a)s"); p["a"] = action
    return conn.execute(f"SELECT count(*) AS c FROM audit_log WHERE {' AND '.join(cond)}",
                        p).fetchone()["c"]


def actors(conn: psycopg.Connection) -> list[dict]:
    """Users who appear as actors in the log, for the filter dropdown."""
    return conn.execute(
        """SELECT DISTINCT a.actor_id, u.email
           FROM audit_log a LEFT JOIN app_user u ON u.id = a.actor_id
           WHERE a.actor_id IS NOT NULL ORDER BY u.email""").fetchall()
