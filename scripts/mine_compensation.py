#!/usr/bin/env python3
"""Distil the collected postings into market pay observations (FR-264).

    python3 scripts/mine_compensation.py --dry-run
    python3 scripts/mine_compensation.py --min-sample 8

Reads the salary-bearing vacancies already in the corpus and publishes one
``posted_vacancies`` observation per (function family, seniority, country), so
the compensation estimator has market evidence instead of only its built-in
prior.  Idempotent: it rebuilds the ``posted_vacancies`` rows.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from dreamjob.db.migrator import migrate  # noqa: E402
from dreamjob.pipeline import compensation_corpus  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--min-sample", type=int, default=compensation_corpus.DEFAULT_MIN_SAMPLE)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    migrate()
    result = compensation_corpus.mine_posted_ranges(
        min_sample=args.min_sample, apply=not args.dry_run
    )
    verb = "would publish" if args.dry_run else "published"
    print(
        f"{verb} {result['segments']} observation(s) from "
        f"{result['vacancies_read']} priced vacancy row(s)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
