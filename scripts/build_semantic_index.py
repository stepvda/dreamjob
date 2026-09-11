#!/usr/bin/env python3
"""Build the semantic index over the corpus (FR-261, FR-282).

    python3 scripts/build_semantic_index.py --entity vacancy
    python3 scripts/build_semantic_index.py --entity company --limit 5000

Requires DREAMJOB_EMBEDDINGS_MODEL; without it the command reports that the
index is unavailable and exits cleanly.  Safe to re-run: unchanged text is
skipped by content hash.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from dreamjob.db.migrator import migrate  # noqa: E402
from dreamjob.pipeline import semantic  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entity", choices=sorted(semantic._TEXT_BUILDERS), default="vacancy")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--batch", type=int, default=64)
    args = parser.parse_args()

    migrate()
    result = semantic.backfill(args.entity, limit=args.limit, batch=args.batch)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
