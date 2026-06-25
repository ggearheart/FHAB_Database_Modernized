#!/usr/bin/env python3
"""Initialize (or re-apply) the FHAB database schema."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fhab.db import DEFAULT_DB_PATH, connect, init_db  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize the FHAB database schema.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="Path to the SQLite database file.")
    args = parser.parse_args()

    conn = connect(args.db)
    init_db(conn)
    conn.close()
    print(f"Schema applied to {args.db}")


if __name__ == "__main__":
    main()
