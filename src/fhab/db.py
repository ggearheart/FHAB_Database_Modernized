"""PostgreSQL connection and schema helpers (psycopg 3)."""

from __future__ import annotations

import os
from pathlib import Path

import psycopg

SQL_DIR = Path(__file__).resolve().parents[2] / "sql"
SCHEMA_PATH = SQL_DIR / "schema.sql"
MIGRATIONS_PATH = SQL_DIR / "migrations.sql"
ACCESS_CONTROL_PATH = SQL_DIR / "access_control.sql"

# Connection string: FHAB_DATABASE_URL or DATABASE_URL (e.g. on Render), else a local
# default suited to the cluster created by scripts/devdb.sh. psycopg accepts URL form.
DEFAULT_DSN = (
    os.environ.get("FHAB_DATABASE_URL")
    or os.environ.get("DATABASE_URL")
    or "dbname=fhab"
)


def connect(dsn: str | None = None) -> psycopg.Connection:
    """Open a connection. Row factory returns dict-like rows."""
    return psycopg.connect(dsn or DEFAULT_DSN, row_factory=psycopg.rows.dict_row)


def apply_schema(conn: psycopg.Connection, schema_path: Path = SCHEMA_PATH) -> None:
    """Apply the schema, forward migrations, and access-control layer. Idempotent.

    Runs schema.sql (CREATE … IF NOT EXISTS), then migrations.sql (ADD COLUMN IF NOT EXISTS,
    bringing an existing database up to date), then access_control.sql.
    """
    conn.execute(schema_path.read_text())
    conn.commit()
    conn.execute(MIGRATIONS_PATH.read_text())
    conn.commit()
    conn.execute(ACCESS_CONTROL_PATH.read_text())
    conn.commit()


def reset_schema(conn: psycopg.Connection) -> None:
    """Drop and recreate the public schema, then reapply. Destructive — dev/test only."""
    conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    conn.commit()
    apply_schema(conn)
