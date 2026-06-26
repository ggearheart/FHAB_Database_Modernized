#!/usr/bin/env python3
"""Download the CA Water Boards HUC-12 watershed layer as GeoJSON (WGS84).

Source: the agency's hosted "HUC Watersheds" feature service (HUC12 layer), which
republishes the USGS Watershed Boundary Dataset. Paginated query; output lands in
data/raw/huc12.geojson (gitignored). See docs/GEOCONNEX.md.
"""

import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlencode

LAYER = ("https://gispublic.waterboards.ca.gov/portalserver/rest/services/"
         "Hosted/HUC_Watersheds/FeatureServer/0/query")
OUT = Path(__file__).resolve().parents[1] / "data" / "raw" / "huc12.geojson"
PAGE = 1000


def _query(offset: int) -> dict:
    params = {
        "where": "1=1",
        "outFields": "huc12,name,hutype,tohuc,areasqkm",
        "returnGeometry": "true",
        "outSR": "4326",
        "orderByFields": "huc12",
        "resultOffset": offset,
        "resultRecordCount": PAGE,
        "f": "geojson",
    }
    url = f"{LAYER}?{urlencode(params)}"
    raw = subprocess.run(["curl", "-fsSL", url], check=True, capture_output=True, text=True).stdout
    return json.loads(raw)


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    features: list[dict] = []
    offset = 0
    while True:
        page = _query(offset)
        feats = page.get("features", [])
        features.extend(feats)
        print(f"  fetched {len(features):,} features…", flush=True)
        if len(feats) < PAGE:
            break
        offset += PAGE
    if not features:
        print("No features fetched.", file=sys.stderr)
        sys.exit(1)
    OUT.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
    print(f"Wrote {len(features):,} HUC-12 features to {OUT} ({OUT.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
