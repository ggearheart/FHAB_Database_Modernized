#!/usr/bin/env python3
"""Regenerate the four published FHAB flat files into a directory.

For scheduled (daily/weekly) generation from cron or a Render cron job, e.g.:

    python scripts/export_flatfiles.py /var/data/fhab_export

By default files are written with their plain published names (bloom-report.csv, etc.).
Pass --dated to also stamp the directory with today's date (fhab_export_YYYY-MM-DD/), matching
the dated-snapshot convention used on data.ca.gov. Uses DATABASE_URL via fhab.db.
"""

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fhab.db import connect  # noqa: E402
from fhab.export import export_all  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="Regenerate the FHAB open-data flat files.")
    ap.add_argument("out_dir", help="Directory to write the CSV files into.")
    ap.add_argument("--dated", action="store_true",
                    help="Write into a date-stamped subdirectory (fhab_export_YYYY-MM-DD).")
    args = ap.parse_args()

    out = Path(args.out_dir)
    if args.dated:
        out = out / f"fhab_export_{date.today().isoformat()}"

    counts = export_all(connect(), out)
    print(f"Wrote {len(counts)} files to {out}:")
    for name, n in counts.items():
        print(f"  {name:20} {n:>8,} rows")


if __name__ == "__main__":
    main()
