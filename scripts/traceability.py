#!/usr/bin/env python3
"""Requirement traceability: which requirement is implemented where.

Scans the source for requirement identifiers cited in docstrings and comments
and reports coverage against the SRS.  The convention the codebase follows is
that a module states the requirements it satisfies in its docstring, so this is
a real index rather than a keyword count - but it measures *citation*, not
correctness.  A requirement can be cited and badly implemented; the tests are
what argue otherwise.

    python3 scripts/traceability.py            # summary
    python3 scripts/traceability.py --full     # every requirement and its files
    python3 scripts/traceability.py --missing  # only the uncited ones
    python3 scripts/traceability.py --json     # machine-readable
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQ_RE = re.compile(r"\b((?:FR|NFR|DR|IR|CR)-\d{3})\b")

SCAN = [
    ("backend/dreamjob", ("*.py", "*.sql", "*.md")),
    ("frontend/src", ("*.jsx", "*.js", "*.css")),
    ("tests", ("*.py",)),
]

# The SRS priority of each requirement, keyed by id.  Kept here so the script
# has no dependency on the .docx being present.
PRIORITY_FILE = ROOT / "docs" / "requirements_index.json"


def load_requirements() -> dict[str, dict]:
    if PRIORITY_FILE.exists():
        return json.loads(PRIORITY_FILE.read_text())
    print(
        f"No requirement index at {PRIORITY_FILE.relative_to(ROOT)}; "
        "reporting citations only.",
        file=sys.stderr,
    )
    return {}


def scan() -> dict[str, set[str]]:
    hits: dict[str, set[str]] = defaultdict(set)
    for folder, patterns in SCAN:
        base = ROOT / folder
        if not base.exists():
            continue
        for pattern in patterns:
            for path in base.rglob(pattern):
                if "__pycache__" in path.parts or "node_modules" in path.parts:
                    continue
                try:
                    text = path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                for rid in set(REQ_RE.findall(text)):
                    hits[rid].add(str(path.relative_to(ROOT)))
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--full", action="store_true", help="list every requirement")
    ap.add_argument("--missing", action="store_true", help="list only uncited ones")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    reqs = load_requirements()
    hits = scan()

    if args.json:
        print(json.dumps({r: sorted(hits.get(r, [])) for r in (reqs or hits)}, indent=1))
        return 0

    known = set(reqs) or set(hits)
    covered = {r for r in known if r in hits}
    missing = sorted(known - covered)

    if args.missing:
        for rid in missing:
            meta = reqs.get(rid, {})
            print(f"{rid} [{meta.get('priority', '?')}] {meta.get('text', '')[:100]}")
        return 0

    if args.full:
        for rid in sorted(known):
            files = sorted(hits.get(rid, []))
            meta = reqs.get(rid, {})
            mark = "OK " if files else "-- "
            print(f"{mark}{rid} [{meta.get('priority', '?')}] {len(files)} file(s)")
            for f in files[:6]:
                print(f"      {f}")
            if len(files) > 6:
                print(f"      … and {len(files) - 6} more")
        print()

    pct = len(covered) * 100 // len(known) if known else 0
    print(f"Requirements in the specification : {len(known)}")
    print(f"Cited in the implementation       : {len(covered)}  ({pct}%)")
    print(f"Not cited                         : {len(missing)}")

    if reqs:
        by_priority = Counter(reqs[r].get("priority", "?") for r in known)
        cov_priority = Counter(reqs[r].get("priority", "?") for r in covered)
        print()
        for code, label in (("M", "Must"), ("S", "Should"), ("C", "Could")):
            total = by_priority.get(code, 0)
            if total:
                got = cov_priority.get(code, 0)
                print(f"  {label:7} {got:3}/{total:3}  ({got * 100 // total}%)")

    if missing:
        print("\nNot cited anywhere:")
        for rid in missing:
            meta = reqs.get(rid, {})
            print(f"  {rid} [{meta.get('priority', '?')}] {meta.get('text', '')[:88]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
