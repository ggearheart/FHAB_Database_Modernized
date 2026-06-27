#!/usr/bin/env python3
"""One-time backfill of report determinations from existing advisory signals."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fhab.backfill import backfill_determination  # noqa: E402
from fhab.db import connect  # noqa: E402

if __name__ == "__main__":
    counts = backfill_determination(connect())
    total = sum(counts.values())
    print(f"Backfilled determination for {total:,} report(s):")
    for code, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {code:18} {n:>6,}")
