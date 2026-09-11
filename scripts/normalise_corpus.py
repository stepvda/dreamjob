#!/usr/bin/env python3
"""Canonicalise the seniority and function labels already in the corpus (FR-261).

    python3 scripts/normalise_corpus.py --dry-run --limit 500
    python3 scripts/normalise_corpus.py

The ATS vendors each ship their own vocabulary - ``experienced``, ``mid_level``,
``entry_level``, ``student_college`` - and those strings were stored as the
FR-261 field, so the largest buckets never matched a directive or a scoring
band.  New collections are canonicalised at synthesis; this corrects the rows
already written, on both ``vacancy`` and the ``opportunity`` that mirrors it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from dreamjob.db.migrator import migrate  # noqa: E402
from dreamjob.pipeline import taxonomy  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None, help="Touch at most N vacancies")
    parser.add_argument("--dry-run", action="store_true", help="Report without writing")
    args = parser.parse_args()

    migrate()
    result = taxonomy.backfill_vacancies(limit=args.limit, apply=not args.dry_run)
    verb = "would rewrite" if args.dry_run else "rewrote"
    print(
        f"{verb} {result['vacancies']} vacancy row(s) and "
        f"{result['opportunities']} opportunity row(s)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
