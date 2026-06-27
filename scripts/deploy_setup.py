#!/usr/bin/env python3
"""One-shot deploy setup: apply the schema + access control and ensure an admin account.

Idempotent — safe to run on every boot. Uses DATABASE_URL (Render) via fhab.db. Set
ADMIN_EMAIL and ADMIN_PASSWORD in the environment to bootstrap the first administrator.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fhab.auth import create_user, grant_role, list_roles_for, set_password  # noqa: E402
from fhab.ceden import ensure_station_registry  # noqa: E402
from fhab.db import apply_schema, connect  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
# Compressed CEDEN station registry committed to the repo so the lab-batch matcher has
# station coordinates on a fresh deploy. Override with CEDEN_STATIONS_CSV (plain or .gz).
DEFAULT_REGISTRY = ROOT / "data" / "ceden_stations.csv.gz"


def main() -> None:
    conn = connect()
    apply_schema(conn)
    print("Schema + access control applied.")

    registry = os.environ.get("CEDEN_STATIONS_CSV", str(DEFAULT_REGISTRY))
    summary = ensure_station_registry(conn, registry)
    if summary.get("loaded"):
        print(f"Station registry loaded: {summary['loaded']:,} stations, "
              f"{summary['enriched']:,} geoms enriched.")
    elif summary.get("already"):
        print(f"Station registry already present ({summary['already']:,} rows) — skipped.")
    else:
        print(f"Station registry not loaded (no file at {registry}).")

    email, password = os.environ.get("ADMIN_EMAIL"), os.environ.get("ADMIN_PASSWORD")
    if email and password:
        uid = create_user(conn, email, "Administrator")
        set_password(conn, uid, password)
        if "program_admin" not in list_roles_for(conn, uid):
            grant_role(conn, uid, "program_admin")
        print(f"Admin account ready: {email}")
    else:
        print("ADMIN_EMAIL/ADMIN_PASSWORD not set — skipping admin bootstrap.")


if __name__ == "__main__":
    main()
