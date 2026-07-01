#!/usr/bin/env python3
"""Ingest a directory of Bend/partner lab email-attachment folders.

Each immediate subdirectory of ROOT is treated as one lab batch (a results CSV plus its
chain-of-custody / transmittal / receipt PDFs). Chemistry is converted to CEDEN long form,
materialized as unlinked samples + results (geocoded from the station registry), and the
original files are stored on the batch for provenance.

    python scripts/ingest_bend_folders.py /path/to/root [--dsn dbname=fhab]

Idempotent on BG_ID: re-running upserts the same samples/results (but appends file copies).
"""

import argparse
import sys
from pathlib import Path

from fhab.bendlab import ingest_bend_folder
from fhab.db import connect


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", type=Path, help="directory whose subfolders are lab batches")
    ap.add_argument("--dsn", default=None)
    args = ap.parse_args()

    conn = connect(args.dsn)
    folders = sorted(p for p in args.root.iterdir() if p.is_dir())
    if not folders:
        print(f"no subfolders under {args.root}", file=sys.stderr)
        return 1

    tot = {"samples": 0, "geocoded": 0, "results": 0, "files": 0, "batches": 0}
    for folder in folders:
        try:
            r = ingest_bend_folder(conn, folder)
        except Exception as e:  # keep going; report the folder that failed
            conn.rollback()
            print(f"  FAILED  {folder.name}: {e}", file=sys.stderr)
            continue
        tot["batches"] += 1
        for k in ("samples", "geocoded", "results", "files"):
            tot[k] += r[k]
        print(f"  batch {r['batch_id']:>3}  {r['region'] or '  -':<10}  "
              f"samples={r['samples']:>2} geocoded={r['geocoded']:>2} "
              f"results={r['results']:>3} files={r['files']}  {r['source']}")

    print(f"\n{tot['batches']} batches: {tot['samples']} samples "
          f"({tot['geocoded']} geocoded), {tot['results']} results, {tot['files']} files stored")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
