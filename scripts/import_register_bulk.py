#!/usr/bin/env python3
"""Stage a national company register's bulk extract (FR-241, FR-341, DR-101).

    python3 scripts/import_register_bulk.py --source kbo --path /data/kbo-extract
    python3 scripts/import_register_bulk.py --source companies_house \
            --path /data/BasicCompanyDataAsOneFile-2026-09-01.csv
    python3 scripts/import_register_bulk.py --coverage

This is an **operator-run importer, not a campaign step**.  The register is the
complete, official, lawful answer to "which companies exist", including the ones
that have never advertised - the only population a speculative opening can be
drawn from.  Downloading it once and staging it beats two million rate-limited
registry lookups, and it is the identity anchor DR-101 asks for.

Staged rows do not become ``company`` rows here.  ``company_seed`` is an index;
a campaign promotes the in-scope slice when it plans (``kbo_bulk`` /
``companies_house_bulk``), so a search never pays for a register it will not
read.  Re-running is a refresh: rows are keyed on ``(source, entity_number)``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from dreamjob.db.migrator import migrate  # noqa: E402
from dreamjob.pipeline import companies_house_bulk, kbo_bulk  # noqa: E402

SOURCES = {
    "kbo": kbo_bulk,
    "companies_house": companies_house_bulk,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=sorted(SOURCES), default="kbo")
    parser.add_argument("--path", default=None, help="Extract directory or CSV file")
    parser.add_argument("--limit", type=int, default=None, help="Stage at most N rows")
    parser.add_argument("--coverage", action="store_true", help="Report and exit")
    args = parser.parse_args()

    migrate()

    if args.coverage or not args.path:
        if not args.path:
            print("No --path given; reporting coverage only.\n")
        for name, module in SOURCES.items():
            print(f"[{name}] coverage: {module.coverage()}")
        return 0

    module = SOURCES[args.source]
    result = module.ingest(args.path, limit=args.limit)
    print(result)
    print(f"[{args.source}] coverage: {module.coverage()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
