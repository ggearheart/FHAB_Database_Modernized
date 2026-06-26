#!/usr/bin/env python3
"""Apply the schema, then optionally load the published open data and re-export it."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fhab.ceden import load_ceden_output  # noqa: E402
from fhab.db import apply_schema, connect, reset_schema  # noqa: E402
from fhab.export import export_all  # noqa: E402
from fhab.loaders import load_open_data  # noqa: E402

REF_DIR = Path(__file__).resolve().parents[1] / "data" / "raw" / "ca_fhab_reference"


def main() -> None:
    p = argparse.ArgumentParser(description="Initialize and (optionally) load the FHAB database.")
    p.add_argument("--reset", action="store_true", help="Drop and recreate the schema first.")
    p.add_argument("--load", action="store_true", help="Load the published flat files after applying schema.")
    p.add_argument("--data-dir", default=str(REF_DIR), help="Directory of the four published CSVs.")
    p.add_argument("--ceden", nargs=2, metavar=("FIELD_CSV", "CHEMISTRY_CSV"),
                   help="Load a Bend->CEDEN output pair (FieldResults, WaterChemistry).")
    p.add_argument("--export", metavar="DIR", help="Re-export the flat files into DIR.")
    args = p.parse_args()

    conn = connect()
    reset_schema(conn) if args.reset else apply_schema(conn)
    print("Schema applied.")

    if args.load:
        print(load_open_data(conn, args.data_dir).summary())
    if args.ceden:
        print(load_ceden_output(conn, args.ceden[0], args.ceden[1]).summary())
    if args.export:
        counts = export_all(conn, args.export)
        print("exported:", ", ".join(f"{k}={v}" for k, v in counts.items()))


if __name__ == "__main__":
    main()
