"""Backfill: score every opportunity that has no score yet (FR-281).

The opportunities in this installation were collected before scoring was wired
into the end of collection, so they carry a null ``score`` and sort to the
bottom of every list.  This walks each campaign that still has unscored rows
and scores them - the deterministic arithmetic only, no model calls - reporting
progress as it goes so a long run is visibly working rather than hung.

    PYTHONPATH=backend python3 scripts/backfill_scoring.py [--email ADDR] [--campaign ID]
"""

from __future__ import annotations

import argparse
import sys
import time

from dreamjob.db.connection import query_one
from dreamjob.db.repositories import campaigns as campaign_repo
from dreamjob.db.repositories import opportunities as opp_repo
from dreamjob.pipeline import scoring

PAGE = 500


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", default="stephane@stepvda.com")
    parser.add_argument("--campaign", default=None)
    args = parser.parse_args()

    seeker = query_one("SELECT id, email FROM job_seeker WHERE email = ?", (args.email,))
    if seeker is None:
        print(f"No account for {args.email}")
        return 1
    sid = seeker["id"]

    if args.campaign:
        campaigns = [campaign_repo.get_campaign_any(args.campaign)]
    else:
        # Derived from the opportunities, not from list_campaigns: that list is
        # bounded, and building the backfill on it skipped a campaign holding
        # 241 unscored rows.
        campaigns = [
            campaign_repo.get_campaign_any(cid)
            for cid in opp_repo.campaign_ids_with_unscored(sid)
        ]

    total_before = opp_repo.count_unscored(sid)
    print(f"{args.email}: {total_before} unscored across {len(campaigns)} campaign(s)", flush=True)

    started = time.time()
    done = 0
    for campaign in campaigns:
        if campaign is None:
            continue
        cid = campaign["id"]
        remaining = opp_repo.count_unscored(sid, cid)
        if not remaining:
            continue
        print(f"\n[{campaign.get('name')}] {remaining} to score", flush=True)
        scored = 0
        while True:
            n = scoring.score_unscored(sid, cid, limit=PAGE)
            if not n:
                break
            scored += n
            done += n
            rate = done / max(1e-9, time.time() - started)
            print(
                f"  scored {scored}/{remaining}  (total {done}/{total_before}, {rate:.0f}/s)",
                flush=True,
            )

    left = opp_repo.count_unscored(sid)
    print(
        f"\nDone: scored {done} in {time.time() - started:.0f}s; "
        f"{left} still unscored",
        flush=True,
    )
    return 0 if left == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
