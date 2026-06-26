#!/usr/bin/env python3
"""Download the CEDEN station lookup list and parse it into a CSV registry.

Source: the CEDEN Data Checker "StationLookUp" list (StationCode -> coordinates), used to
enrich station.geom so CEDEN/Bend samples can be spatially linked to FHAB events.
Output lands in data/raw/ceden_stations.csv (gitignored).
"""

import csv
import re
import subprocess
import sys
from pathlib import Path

URL = "https://ceden.org/CEDEN_Checker/Checker/DisplayCEDENLookUp.php?List=StationLookUp"
OUT = Path(__file__).resolve().parents[1] / "data" / "raw" / "ceden_stations.csv"
COLUMNS = ["StationCode", "StationName", "StationSource", "CoordinateSource",
           "CoordinateNumber", "TargetLatitude", "TargetLongitude", "Datum", "LastUpdateDate"]

_ROW = re.compile(r"<tr>(.*?)</tr>", re.S)
_CELL = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_TAG = re.compile(r"<.*?>")


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    print(f"Fetching {URL} …")
    html = subprocess.run(["curl", "-fsSL", URL], check=True, capture_output=True, text=True).stdout

    n = 0
    with OUT.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(COLUMNS)
        for row in _ROW.findall(html):
            cells = [_TAG.sub("", c).strip() for c in _CELL.findall(row)]
            if len(cells) >= 8:
                writer.writerow((cells + [""] * 9)[:9])
                n += 1
    if n == 0:
        print("No station rows parsed — page format may have changed.", file=sys.stderr)
        sys.exit(1)
    print(f"Wrote {n:,} stations to {OUT}")


if __name__ == "__main__":
    main()
