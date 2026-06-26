#!/usr/bin/env python3
"""Download the published CA FHAB flat files and data dictionary into data/raw/.

These are the State Water Board's Surface Water — Freshwater Harmful Algal Blooms
resources on the California Open Data Portal. They serve as reference data and the
export target for the modernized schema. Files land in data/raw/ (gitignored).

The snapshot date is part of each CSV's filename and changes over time; this script
follows the dataset's stable resource URLs, which always serve the latest snapshot.
"""

import shutil
import subprocess
import sys
from pathlib import Path

DATASET = "https://data.ca.gov/dataset/surface-water-freshwater-harmful-algal-blooms"
BASE = "https://data.ca.gov/dataset/ab672540-aecd-42f1-9b05-9aad326f97ec/resource"

# (local filename, resource path)
RESOURCES = [
    ("habs-master-data-dictionary.pdf", "516bd039-e09c-4e2f-aa65-eda947af729b/download/habs-master-data-dictionary.pdf"),
    ("habs-disclaimer.pdf", "9a54fa52-e16f-4943-b27d-2b4eca7c1bd6/download/habs-disclaimer-for-data-dictionary.pdf"),
    ("bloom_reports.csv", "c6a36b91-ad38-4611-8750-87ee99e497dd/download/bloom-report_2026-06-02.csv"),
    ("cases.csv", "67648948-034f-4882-bbc0-c07c7d38daf9/download/hab-cases_2026-06-02.csv"),
    ("responses.csv", "4283c060-c22f-48f5-a75c-8bccf0c54a99/download/hab-responses_2026-06-02.csv"),
    ("results.csv", "9d4e1df4-0cd6-4165-9e63-effcafd9dccc/download/hab-results_2026-06-02.csv"),
]

DEST = Path(__file__).resolve().parents[1] / "data" / "raw" / "ca_fhab_reference"


def _download(url: str, out: Path) -> int:
    # The portal's WAF blocks Python's urllib client regardless of User-Agent, but
    # serves curl normally — so shell out to curl, which is available on macOS/Linux.
    if not shutil.which("curl"):
        raise RuntimeError("curl is required but was not found on PATH")
    subprocess.run(
        ["curl", "-fsSL", "-o", str(out), url],
        check=True,
        capture_output=True,
    )
    return out.stat().st_size


def main() -> None:
    DEST.mkdir(parents=True, exist_ok=True)
    print(f"Source dataset: {DATASET}")
    for name, path in RESOURCES:
        url = f"{BASE}/{path}"
        out = DEST / name
        print(f"  fetching {name} …", end=" ", flush=True)
        try:
            print(f"{_download(url, out):,} bytes")
        except Exception as exc:  # noqa: BLE001
            print(f"FAILED: {exc}", file=sys.stderr)
    print(f"\nSaved to {DEST}")


if __name__ == "__main__":
    main()
