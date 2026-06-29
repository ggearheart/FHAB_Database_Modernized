#!/usr/bin/env python3
"""Delete ALL lab data: samples, results, the sample-link matcher rows, and the lab-batch
staging tables. Keeps reports/events/cases, the public submission queue, the analyte vocabulary,
and the CEDEN station registry/stations.

Dry-run by default (prints the row counts it WOULD delete). Pass --yes to actually delete.
Uses DATABASE_URL (or FHAB_DATABASE_URL) via fhab.db — on Render the Shell already has it set:

    python scripts/purge_lab_data.py          # dry run — shows counts
    python scripts/purge_lab_data.py --yes     # delete

Irreversible. Make sure you have a backup if the data matters.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fhab.db import connect  # noqa: E402
from fhab.maintenance import KEPT_TABLES, LAB_TABLES, lab_data_counts, purge_lab_data  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="Purge all lab data (samples/results/staging).")
    ap.add_argument("--yes", action="store_true", help="actually delete (otherwise dry-run)")
    args = ap.parse_args()

    conn = connect()
    counts = lab_data_counts(conn)
    print("Lab-data tables to clear (rows):")
    total = sum(counts[t] for t in LAB_TABLES)
    for t in LAB_TABLES:
        print(f"  {t:18} {counts[t]:>9,}")
    print(f"  {'TOTAL':18} {total:>9,}")

    if not args.yes:
        print("\nDry run — nothing deleted. Re-run with --yes to delete the rows above.")
        return

    purge_lab_data(conn)
    after = lab_data_counts(conn)
    print("\nDeleted. Remaining in cleared tables:")
    for t in LAB_TABLES:
        print(f"  {t:18} {after[t]:>9,}")
    print("Preserved (untouched):")
    for t in KEPT_TABLES:
        print(f"  {t:18} {after[t]:>9,}")


if __name__ == "__main__":
    main()
