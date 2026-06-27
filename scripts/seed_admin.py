#!/usr/bin/env python3
"""Create (or update) a program_admin account for the staff web app.

Run once to bootstrap the first administrator, who can then create other accounts in the UI.

    python scripts/seed_admin.py --email admin@fhab.local --password "change-me" --name "Site Admin"
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fhab.auth import create_user, grant_role, set_password  # noqa: E402
from fhab.db import connect  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description="Seed a program_admin web account.")
    p.add_argument("--email", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--name", default="Administrator")
    args = p.parse_args()

    conn = connect()
    uid = create_user(conn, args.email, args.name)
    set_password(conn, uid, args.password)
    grant_role(conn, uid, "program_admin")
    print(f"Admin account ready: {args.email} (id={uid}). Sign in at /login.")


if __name__ == "__main__":
    main()
