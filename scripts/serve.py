#!/usr/bin/env python3
"""Run the FHAB staff web app (dev server)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fhab.web import create_app  # noqa: E402

if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Run the FHAB staff web app.")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5000)
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()

    create_app().run(host=args.host, port=args.port, debug=args.debug)
