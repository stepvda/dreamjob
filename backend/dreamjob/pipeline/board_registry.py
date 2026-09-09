"""What the board registry has learned, in the shape the pipeline asks for.

The registry is a harvested list: ~4,500 ATS slugs pulled out of Common Crawl,
the Wayback index and Hacker News in about 65 requests, which is the only
affordable way to turn "which employers exist" into fetchable plan items
(docs/Data_Gathering_Plan.md section 5.2).  Harvested lists decay, measurably.
The design measured 87.5% liveness for a slug seen in a fresh Common Crawl and
27.5% for one seen only in Wayback; this installation has now measured its own
number, across the 2,227 boards it has actually read:

    1,671  returned vacancies
      232  answered and held none
      208  answered HTTP 404
      116  ended some other way

85.4% live, which is the design's number.  The 208 are not failures - a company
that closed its board is a fact about the world, and a registry built from
public indexes is *expected* to name some of them.  They became a problem only
because nothing wrote the fact down: all 208 were offered to all 175 campaigns,
and would have been offered to the next one.

This module is where a stage says what it saw and asks what is still worth
fetching.  The SQL is in ``db/repositories/board_registry`` because that is the
house rule and because a move off SQLite has to stay feasible (CR-408); this is
the pipeline-facing name for it, plus the one piece of policy that is genuinely
the pipeline's - subtracting the retired boards from the shipped registry file
before discovery turns it into plan items.

The contract, for the slices that call it:

* **collection**, when an ATS board answers 404 or 410::

      from dreamjob.pipeline.board_registry import mark_gone
      mark_gone(vendor, slug, http_status)     # -> 'unverified' | 'gone'

  Two of those retire the slug (:data:`RETIRE_AFTER_FAILURES`).  A 5xx, a
  timeout or a parse crash is *not* this call - it is ``mark_unreachable``,
  which records the status and leaves the registry's opinion alone, because a
  broken server says nothing about whether the tenant exists.  That distinction
  is the registry's half of the four outcomes a plan item can end in:
  ``succeeded | blocked | gone | failed``.

* **discovery / planning**, when building board targets::

      from dreamjob.pipeline.board_registry import live_slugs, without_gone
      slugs = live_slugs("greenhouse")         # slugs still worth a request
      rows  = without_gone(load_board_registry())   # the file, minus the dead

  Either one keeps a retired slug out of the plan.  Neither is a filter on
  "verified live": an unverified board - which is 4,515 of 4,518 rows here -
  has simply never been asked, and dropping those would leave three targets.

Requirements: FR-181 (the registry is the route to employers), FR-186 (a cap
should be spent on boards that answer), FR-343 (staleness per record type).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from dreamjob.db.repositories.board_registry import (
    GONE_STATUSES,
    RETIRE_AFTER_FAILURES,
    REVISIT_AFTER_DAYS,
    STATES,
    boards,
    due_for_revisit,
    live_slugs,
    mark_gone,
    mark_live,
    mark_unreachable,
    record_verification,
    retired_keys,
    state_of,
    summary,
)

log = logging.getLogger(__name__)

__all__ = [
    "GONE_STATUSES",
    "RETIRE_AFTER_FAILURES",
    "REVISIT_AFTER_DAYS",
    "STATES",
    "boards",
    "due_for_revisit",
    "live_slugs",
    "mark_gone",
    "mark_live",
    "mark_unreachable",
    "record_verification",
    "retired_keys",
    "state_of",
    "summary",
    "without_gone",
]


def without_gone(
    rows: Iterable[dict[str, Any]], now: datetime | None = None
) -> list[dict[str, Any]]:
    """The shipped registry file, minus the boards this installation retired.

    ``discovery.load_board_registry`` reads a committed JSON file, and that file
    cannot know what this installation's own requests found: it ships
    ``last_verified: null`` on every row by design.  This is the subtraction
    that turns it into targets worth fetching.

    Both spellings of the key are read (``ats_vendor`` from discovery,
    ``vendor`` from the importer and the raw file), because a filter that reads
    only one of them silently filters nothing.
    """
    retired = retired_keys(now)
    if not retired:
        return list(rows)
    kept: list[dict[str, Any]] = []
    dropped = 0
    for row in rows:
        vendor = str(row.get("ats_vendor") or row.get("vendor") or "").strip().lower()
        slug = str(row.get("ats_slug") or row.get("slug") or "").strip().strip("/")
        if (vendor, slug.lower()) in retired:
            dropped += 1
            continue
        kept.append(row)
    if dropped:
        log.info(
            "Board registry: %d retired board(s) left out of the plan; "
            "they are re-tested after %d days",
            dropped, REVISIT_AFTER_DAYS,
        )
    return kept
