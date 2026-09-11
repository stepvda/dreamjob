"""The six answers a finished plan item can end on (FR-185, NFR-403).

This is the pure half of the dashboard's outcome ledger.  It lives in the
pipeline rather than in the API router for one reason: ``collection.status()``
now computes the ledger over *every* plan item before it trims the per-source
list down to the page it sends, so the router and the pipeline have to share
one implementation of the classification.  Everything here is a pure function
over rows, so it can be imported from either side without a cycle and tested
without a database.

A campaign that reports "538 errors" is not reporting anything.  308 of those
were ATS boards answering 404 - the measured cost of a registry harvested from
Common Crawl and Wayback, where a Wayback-only slug is live 27.5% of the time.
192 were robots.txt refusals, which is this product declining a source on
purpose (FR-182, CR-402) and is the opposite of a defect.  Burying the dozen
real failures among five hundred expected outcomes is how the next real failure
gets missed.

So a finished plan item is sorted into one of six answers.  Only ``failed``
means something went wrong, and nothing else may be folded into it - that is
the whole point, and it is why the dashboard can afford to shout about it.

The counterweight, and it matters: the previous bug here was the opposite one.
A plan item that fetched nothing, was blocked, or crashed was written down as
"done, 0 records, 0 errors", and that single lie hid a total retrieval failure
for hours.  Nothing below may quiet a failure: an answer this module does not
recognise is counted as ``failed``, never as success.
"""

from __future__ import annotations

#: The six answers, in the order the dashboard reads them.
OUTCOME_STATES = ("succeeded", "blocked", "gone", "failed", "skipped", "capped")

#: ... plus the two that mean "no answer yet".
PENDING_STATES = ("running", "pending")

#: How many individual failures the payload names before it starts counting
#: instead.  ``failed`` is the list an operator reads line by line, so it gets
#: room; the other states are grouped per adapter and need none.
FAILED_DETAIL_LIMIT = 60

#: Distinct reasons carried per grouped adapter.  Generous rather than
#: illustrative for ``blocked``: "we declined 192 sources on principle" is only
#: defensible if the principle can be read source by source, and each reason
#: names the URL that was refused.  Groups that are merely informative get the
#: short list.
GROUP_REASON_LIMIT = 25
SHORT_REASON_LIMIT = 3

#: Attempts an adapter needs before "every slug came back 404" reads as adapter
#: breakage rather than as registry decay (NFR-403).  Below this the sample is
#: too small to tell the two apart, and saying so is better than guessing.
BREAKAGE_MIN_ATTEMPTS = 5

#: The measured outcome of a source, as the collection worker writes it, mapped
#: onto the answer the operator is shown.  Unknown states are deliberately not
#: defaulted here: see ``_bucket_of``.
_STATE_BUCKET: dict[str, str] = {
    "succeeded": "succeeded",
    # Declined, correctly: robots.txt (FR-182), a 403 bot wall, or terms of
    # service (IR-101).  A decision the product should be able to defend, and
    # therefore a thing to show rather than a thing to count as breakage.
    "blocked": "blocked",
    "refused": "blocked",
    "robots_disallowed": "blocked",
    "tos_prohibited": "blocked",
    # The target is not there any more - 404 or 410 on a board the registry
    # believed was live.  The registry learns this once and stops offering it.
    "gone": "gone",
    "not_found": "gone",
    "retired": "gone",
    # The page budget stopped it (FR-186): a budget signal, not an outcome.
    "capped": "capped",
    # Nothing to do.  ``no_matches`` belongs here rather than under
    # ``succeeded``: the source answered and said it holds nothing for this
    # query, which is a completed unit of work but not a collected record.
    "no_work": "skipped",
    "skipped": "skipped",
    "no_matches": "skipped",
    # Something actually went wrong.  ``extracted_nothing`` is in this list on
    # purpose: a source that was fetched and yielded no record is an adapter
    # that has stopped matching the page it reads (NFR-403).
    "failed": "failed",
    "rejected": "failed",
    "normalised_nothing": "failed",
    "extracted_nothing": "failed",
}

#: The fall-back when a plan item carries no measured outcome, keyed on the
#: status column.  ``done`` is absent because it alone needs the record count to
#: separate a source that did its job from one that had nothing to do.
_STATUS_BUCKET: dict[str, str] = {
    "failed": "failed",
    "blocked": "blocked",
    "gone": "gone",
    "capped": "capped",
    "skipped": "skipped",
}

#: How the collection worker says "the page budget stopped this one" today: the
#: item stays ``planned`` so that raising the cap continues it from its
#: checkpoint, and the reason is written into ``last_error`` citing FR-186.
#: Reading that marker is what tells 4,664 items the budget held back from the
#: 45,000 that simply have not been reached yet.  A ``capped`` state or status
#: supersedes it the moment one arrives - both are already mapped above.
_CAP_MARKERS = ("not started: ", "stopped by cap: ")


def _capped_by_budget(source: dict) -> bool:
    """FR-186: was this item held back by a cap rather than never planned?"""
    reason = source.get("last_error") or ""
    return "FR-186" in reason and reason.startswith(_CAP_MARKERS)


def _bucket_of(source: dict) -> str:
    """Which of the six answers this plan item ended on (FR-185)."""
    status = source.get("status")
    if status in ("planned", "running", None):
        # Not finished, so not an outcome: waiting, running, excluded by the job
        # seeker (FR-163), or held back by the page budget (FR-186).
        if source.get("excluded_by_user"):
            return "skipped"
        if status == "running":
            return "running"
        return "capped" if _capped_by_budget(source) else "pending"
    # ``outcome_state`` (the column migration 130 added and the collection
    # worker maintains) before ``outcome`` (the older copy inside ``caps``).
    # For an item settled since that migration the two agree.  For the 504
    # items it *relabelled* from the evidence already in ``last_error`` - 293
    # robots refusals and 211 dead boards in one campaign - only the column was
    # rewritten, and the ``caps`` blob still carries the word the pre-fix code
    # wrote there: ``failed``.  Reading ``caps`` first put every one of them
    # back into the failure count, which is the number this whole screen exists
    # to keep honest, so the migrated column wins.
    state = source.get("outcome_state") or source.get("outcome")
    if state in _STATE_BUCKET:
        return _STATE_BUCKET[state]
    if status in _STATUS_BUCKET:
        return _STATUS_BUCKET[status]
    if status == "done":
        return "succeeded" if source.get("records_collected") else "skipped"
    # An answer nobody taught this endpoint about.  It is not quietly a success:
    # writing an unrecognised end state down as "done" is the bug this whole
    # module exists to keep from coming back.
    return "failed"


def _shorten(text: str | None, limit: int = 180) -> str | None:
    if not text:
        return None
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _group(
    rows: list[dict], catalogue: dict[str, dict], reason_limit: int = SHORT_REASON_LIMIT
) -> list[dict]:
    """Collapse per-item outcomes into one row per adapter, with the reasons.

    Nobody reads 308 dead slugs one at a time, so the unit shown is the adapter.
    The reasons are carried verbatim underneath it - each one names the URL that
    was refused or that answered 404 - and the count of the ones that did not
    fit is carried too, so the list never pretends to be complete when it is not.
    """
    grouped: dict[str, dict] = {}
    seen: dict[str, set[str]] = {}
    for source in rows:
        key = source["adapter_key"]
        entry = grouped.get(key)
        if entry is None:
            catalogue_row = catalogue.get(key) or {}
            seen[key] = set()
            entry = grouped[key] = {
                "adapter_key": key,
                "display_name": source.get("display_name") or key,
                "count": 0,
                "reasons": [],
                "reasons_omitted": 0,
                # IR-101: whether this source is one an administrator has had to
                # acknowledge belongs beside the refusal, because it is half the
                # answer to "why did you not collect this?".
                "tos_status": catalogue_row.get("tos_status"),
                "requires_ack": bool(catalogue_row.get("requires_ack")),
                "acknowledged_at": catalogue_row.get("acknowledged_at"),
            }
        entry["count"] += 1
        reason = _shorten(source.get("last_error"))
        if reason and reason not in seen[key]:
            seen[key].add(reason)
            if len(entry["reasons"]) < reason_limit:
                entry["reasons"].append(reason)
            else:
                entry["reasons_omitted"] += 1
    return sorted(grouped.values(), key=lambda row: (-row["count"], row["adapter_key"]))


def collection_outcomes(sources: list[dict], catalogue: dict[str, dict]) -> dict:
    """Sort the plan items into the six answers, with the detail behind each.

    ``failed`` is listed item by item because that is the list somebody works
    through.  ``blocked`` and ``gone`` are grouped per adapter because nobody
    reads 308 dead slugs one at a time - but the group carries examples, and for
    ``gone`` it carries the share, which is what separates a registry decaying
    at its measured rate from an adapter that has broken (NFR-403).
    """
    buckets: dict[str, list[dict]] = {name: [] for name in OUTCOME_STATES}
    counts = dict.fromkeys(OUTCOME_STATES + PENDING_STATES, 0)
    attempts: dict[str, int] = {}
    gone_by_adapter: dict[str, int] = {}
    records = 0
    producing = 0
    errors = 0

    for source in sources:
        bucket = _bucket_of(source)
        counts[bucket] += 1
        collected = source.get("records_collected") or 0
        records += collected
        errors += source.get("error_count") or 0
        if collected:
            producing += 1
        if bucket in buckets:
            buckets[bucket].append(source)
        if bucket in ("succeeded", "blocked", "gone", "failed"):
            # Items the run actually put a request behind.  A capped or pending
            # item never asked, so counting it would dilute the share below.
            key = source["adapter_key"]
            attempts[key] = attempts.get(key, 0) + 1
            if bucket == "gone":
                gone_by_adapter[key] = gone_by_adapter.get(key, 0) + 1

    failed = [
        {
            "plan_item_id": source.get("plan_item_id"),
            "adapter_key": source["adapter_key"],
            "display_name": source.get("display_name") or source["adapter_key"],
            "state": source.get("outcome_state") or source.get("outcome"),
            "reason": _shorten(source.get("outcome_reason") or source.get("last_error")),
            "error_count": source.get("error_count") or 0,
            "extraction_success_rate": source.get("extraction_success_rate"),
        }
        for source in sorted(
            buckets["failed"], key=lambda s: (-(s.get("error_count") or 0), s["adapter_key"])
        )[:FAILED_DETAIL_LIMIT]
    ]

    gone = _group(buckets["gone"], catalogue, GROUP_REASON_LIMIT)
    for row in gone:
        attempted = attempts.get(row["adapter_key"], row["count"])
        row["attempted"] = attempted
        row["share"] = row["count"] / attempted if attempted else None
        # NFR-403: a registry harvested from Wayback is 27.5% live, so dead
        # slugs are expected.  An adapter where *every* slug of a decent sample
        # is dead is not decay - the adapter or its URL shape has broken, and
        # that is a different problem with a different fix.
        row["suspected_breakage"] = (
            attempted >= BREAKAGE_MIN_ATTEMPTS and row["count"] == attempted
        )

    return {
        "counts": counts,
        "records": records,
        "producing_sources": producing,
        # The number the dashboard used to headline, kept because a per-page
        # retry that eventually succeeded still costs something worth seeing.
        "error_count": errors,
        "failed": failed,
        "failed_listed": len(failed),
        # FR-182, CR-402, IR-101: the refusals are the one list an operator
        # may have to defend line by line, so they are the least abridged.
        "blocked": _group(buckets["blocked"], catalogue, GROUP_REASON_LIMIT),
        "gone": gone,
        "capped": _group(buckets["capped"], catalogue),
        "skipped": _group(buckets["skipped"], catalogue),
    }
