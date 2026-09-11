"""Stage a KBO open-data extract, and optionally select from it (FR-161, FR-143).

Belgium publishes its company register as a monthly open-data download: a zip
of CSVs at https://kbopub.economie.fgov.be/kbo-open-data. Unzip it and point
this at the directory.

    PYTHONPATH=backend python3 scripts/ingest_kbo.py /path/to/unzipped
    PYTHONPATH=backend python3 scripts/ingest_kbo.py --coverage
    PYTHONPATH=backend python3 scripts/ingest_kbo.py --select "information technology"

Staging is the whole registry; ``--select`` promotes only the in-scope slice
into the knowledge base, which is what a campaign then plans against.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dreamjob.db.connection import query_one
from dreamjob.db.migrator import migrate
from dreamjob.pipeline import kbo_bulk, sectors
from dreamjob.pipeline.directives import DirectiveSetPayload, JobContentDirectives


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage the Belgian company register.")
    parser.add_argument("directory", nargs="?", help="the unzipped KBO open-data directory")
    parser.add_argument("--coverage", action="store_true", help="report what is staged")
    parser.add_argument("--limit", type=int, default=None, help="stage at most N entities")
    parser.add_argument(
        "--select",
        metavar="INDUSTRY",
        action="append",
        default=None,
        help="promote companies in this industry into the knowledge base (repeatable)",
    )
    parser.add_argument("--select-limit", type=int, default=100)
    args = parser.parse_args()

    migrate()

    if args.directory:
        directory = Path(args.directory)
        if not directory.is_dir():
            print(f"Not a directory: {directory}")
            return 1
        print(f"Staging from {directory} ...")
        result = kbo_bulk.ingest(directory, limit=args.limit)
        print(
            f"  staged {result['staged']} of {result['entities']} entities"
            f" ({result['skipped_without_a_name']} had no name)"
        )
        if result["top_divisions"]:
            top = ", ".join(f"{code}={n}" for code, n in result["top_divisions"].items())
            print(f"  largest divisions: {top}")
    elif not args.coverage and not args.select:
        parser.print_help()
        return 0

    if args.select:
        payload = DirectiveSetPayload(
            name="CLI selection",
            job_content=JobContentDirectives(industries_include=list(args.select)),
        )
        divisions = sectors.directive_divisions(payload)
        print(f"Selecting for {', '.join(args.select)} -> NACE divisions {divisions or '(all)'}")
        result = kbo_bulk.select_for_directives(payload, limit=args.select_limit)
        print(f"  in scope {result['selected']}, promoted {result['materialised']}")
        total = query_one("SELECT COUNT(*) AS n FROM company WHERE legal_id_type = 'kbo'")
        print(f"  knowledge-base companies from the register: {total['n']}")

    if args.coverage or not args.directory:
        coverage = kbo_bulk.coverage()
        print("\nStaged universe:")
        print(f"  seeded           {coverage['seeded']}")
        print(f"  active or unknown{coverage['active_or_unknown']:>7}")
        print(f"  materialised     {coverage['materialised']}")
        print(f"  {coverage['note']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
