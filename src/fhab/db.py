"""Database connection and schema initialization helpers."""

from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path("fhab.db")
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "sql" / "schema.sql"


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open a connection with foreign keys enabled and row access by name."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection, schema_path: Path = SCHEMA_PATH) -> None:
    """Apply the schema. Idempotent — uses CREATE TABLE IF NOT EXISTS."""
    conn.executescript(schema_path.read_text())
    conn.commit()
