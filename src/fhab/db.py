"""PostgreSQL connection and schema helpers (psycopg 3)."""

from __future__ import annotations

import os
from pathlib import Path

import psycopg

SQL_DIR = Path(__file__).resolve().parents[2] / "sql"
SCHEMA_PATH = SQL_DIR / "schema.sql"
ACCESS_CONTROL_PATH = SQL_DIR / "access_control.sql"

# Connection comes from the standard libpq env vars (PGHOST, PGDATABASE, …) or a single
# FHAB_DATABASE_URL. Defaults suit a local dev cluster created by scripts/devdb.sh.
DEFAULT_DSN = os.environ.get("FHAB_DATABASE_URL", "dbname=fhab")


def connect(dsn: str | None = None) -> psycopg.Connection:
    """Open a connection. Row factory returns dict-like rows."""
    return psycopg.connect(dsn or DEFAULT_DSN, row_factory=psycopg.rows.dict_row)


def apply_schema(conn: psycopg.Connection, schema_path: Path = SCHEMA_PATH) -> None:
    """Apply the schema and access-control layer. Idempotent."""
    conn.execute(schema_path.read_text())
    conn.commit()
    conn.execute(ACCESS_CONTROL_PATH.read_text())
    conn.commit()


def reset_schema(conn: psycopg.Connection) -> None:
    """Drop and recreate the public schema, then reapply. Destructive — dev/test only."""
    conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    conn.commit()
    apply_schema(conn)
