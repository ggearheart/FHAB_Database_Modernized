#!/usr/bin/env python3
"""Download the published CA FHAB flat files and data dictionary into data/raw/.

These are the State Water Board's Surface Water — Freshwater Harmful Algal Blooms
resources on the California Open Data Portal. They serve as reference data and the
export target for the modernized schema. Files land in data/raw/ (gitignored).

The published CSV filenames include the snapshot date (e.g. bloom-report_2026-06-26.csv),
which changes whenever the portal republishes — so we resolve each resource's *current*
download URL from the CKAN API (the resource IDs are stable) rather than hardcoding it.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

DATASET = "https://data.ca.gov/dataset/surface-water-freshwater-harmful-algal-blooms"
API = "https://data.ca.gov/api/3/action/resource_show"

# (local filename, stable CKAN resource id)
RESOURCES = [
    ("habs-master-data-dictionary.pdf", "516bd039-e09c-4e2f-aa65-eda947af729b"),
    ("habs-disclaimer.pdf", "9a54fa52-e16f-4943-b27d-2b4eca7c1bd6"),
    ("bloom_reports.csv", "c6a36b91-ad38-4611-8750-87ee99e497dd"),
    ("cases.csv", "67648948-034f-4882-bbc0-c07c7d38daf9"),
    ("responses.csv", "4283c060-c22f-48f5-a75c-8bccf0c54a99"),
    ("results.csv", "9d4e1df4-0cd6-4165-9e63-effcafd9dccc"),
]

DEST = Path(__file__).resolve().parents[1] / "data" / "raw" / "ca_fhab_reference"


def _curl(url: str) -> str:
    # The portal's WAF blocks Python's urllib regardless of User-Agent, but serves curl.
    return subprocess.run(["curl", "-fsSL", url], check=True, capture_output=True, text=True).stdout


def _resource_url(resource_id: str) -> str:
    """Resolve a resource's current download URL via the CKAN resource_show API."""
    data = json.loads(_curl(f"{API}?id={resource_id}"))
    return data["result"]["url"]


def _download(url: str, out: Path) -> int:
    subprocess.run(["curl", "-fsSL", "-o", str(out), url], check=True, capture_output=True)
    return out.stat().st_size


def main() -> None:
    if not shutil.which("curl"):
        raise SystemExit("curl is required but was not found on PATH")
    DEST.mkdir(parents=True, exist_ok=True)
    print(f"Source dataset: {DATASET}")
    failures = 0
    for name, resource_id in RESOURCES:
        out = DEST / name
        print(f"  fetching {name} …", end=" ", flush=True)
        try:
            print(f"{_download(_resource_url(resource_id), out):,} bytes")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAILED: {exc}", file=sys.stderr)
    print(f"\nSaved to {DEST}")
    if failures:
        raise SystemExit(f"{failures} resource(s) failed to download.")


if __name__ == "__main__":
    main()
