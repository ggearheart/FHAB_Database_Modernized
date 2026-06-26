#!/usr/bin/env python3
"""Enter a new FHAB bloom report from the terminal, acting as a given user (under RLS).

Examples:
    # Enter a report as an existing staffer
    python scripts/enter_report.py --as test.staff@waterboards.ca.gov \
        --waterbody "Test Pond" --region "Region 5 - Central Valley" --county Sacramento \
        --lat 38.5816 --lon -121.4944 --bloom-type cyanobacteria --bloom-size "small" \
        --description "Green scum near the boat ramp."

    # Create the identity first, then enter (staff)
    python scripts/enter_report.py --as jo@wb.ca.gov --ensure-role wb_staff \
        --region "Region 5 - Central Valley" --waterbody "Test Pond" ...

Report intake (creating a new waterbody + location) is a staff function; this tool is for
staff/admin roles. Contributors submit through the stations/samples/results path instead.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg  # noqa: E402

from fhab.auth import create_user, grant_role  # noqa: E402
from fhab.db import connect  # noqa: E402
from fhab.reports import enter_report  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description="Enter a bloom report as a user, under access control.")
    p.add_argument("--as", dest="email", required=True, help="Acting user's email.")
    p.add_argument("--ensure-role", help="Create the user with this role if they don't exist.")
    p.add_argument("--region", help="Scope/region (for --ensure-role and new waterbodies).")
    p.add_argument("--org", help="Org scope for a contributor role (with --ensure-role).")
    p.add_argument("--waterbody", required=True, help="Water body name.")
    p.add_argument("--county")
    p.add_argument("--lat", type=float)
    p.add_argument("--lon", type=float)
    p.add_argument("--date", help="Observation date (YYYY-MM-DD); defaults to today.")
    p.add_argument("--report-type", default="Staff entry")
    p.add_argument("--bloom-type")
    p.add_argument("--bloom-size")
    p.add_argument("--bloom-location")
    p.add_argument("--bloom-texture")
    p.add_argument("--description")
    p.add_argument("--owner-org", help="Owning organization (for contributor-submitted reports).")
    args = p.parse_args()

    conn = connect()
    row = conn.execute("SELECT id FROM app_user WHERE email = %s", (args.email,)).fetchone()
    if row:
        user_id = row["id"]
    elif args.ensure_role:
        user_id = create_user(conn, args.email)
        grant_role(conn, user_id, args.ensure_role, region=args.region, org=args.org)
        print(f"Created user {args.email} with role {args.ensure_role}.")
    else:
        sys.exit(f"No user '{args.email}'. Pass --ensure-role to create one.")

    try:
        rid = enter_report(
            conn, user_id,
            water_body_name=args.waterbody, region=args.region, county=args.county,
            lat=args.lat, lon=args.lon, observation_date=args.date,
            report_type=args.report_type, bloom_type=args.bloom_type, bloom_size=args.bloom_size,
            bloom_location=args.bloom_location, bloom_texture=args.bloom_texture,
            description=args.description, owner_org=args.owner_org,
        )
    except psycopg.errors.InsufficientPrivilege:
        sys.exit(f"Access denied: {args.email} may not file this report (role/region/owner policy).")
    except psycopg.Error as exc:
        sys.exit(f"Could not enter report: {str(exc).splitlines()[0]}")

    print(f"Report entered: Bloom_Report_ID = {rid} (filed by {args.email})")


if __name__ == "__main__":
    main()
