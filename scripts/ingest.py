#!/usr/bin/env python3
"""Ingest a long-format FHAB CSV into the database."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fhab.db import DEFAULT_DB_PATH, connect, init_db  # noqa: E402
from fhab.ingest import ingest_csv  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest a FHAB CSV into the database.")
    parser.add_argument("csv", help="Path to the long-format CSV file.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="Path to the SQLite database file.")
    parser.add_argument(
        "--strict", action="store_true", help="Exit non-zero if any row is skipped."
    )
    args = parser.parse_args()

    conn = connect(args.db)
    init_db(conn)  # ensure schema exists
    report = ingest_csv(conn, args.csv)
    conn.close()

    print(report.summary())
    if args.strict and not report.ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
