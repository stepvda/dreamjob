"""The six answers the campaign dashboard reports (FR-185, FR-361, NFR-403).

The dashboard said "538 errors" for a run in which about a dozen things had
actually gone wrong.  These tests hold the two halves of the fix apart, because
the pendulum has swung both ways here:

* an expected outcome - a board that no longer exists, a source declined on
  principle, work the page budget never started - must not be counted as a
  failure, or the failures cannot be found;
* and nothing may be quietened.  The previous bug wrote a source that fetched
  nothing down as "done, 0 records, 0 errors" and hid a total retrieval failure
  behind it, so an end state this code does not recognise counts as ``failed``.

``collection_outcomes`` is a pure function over the status payload, so these
run without a database.
"""

from __future__ import annotations

from dreamjob.api.routers.campaigns import collection_outcomes


def _source(**overrides) -> dict:
    source = {
        "plan_item_id": overrides.pop("plan_item_id", "item"),
        "adapter_key": "ats.greenhouse",
        "display_name": "Greenhouse",
        "status": "done",
        "outcome": "succeeded",
        "records_collected": 0,
        "error_count": 0,
        "last_error": None,
        "excluded_by_user": False,
    }
    source.update(overrides)
    return source


def _counts(sources: list[dict], catalogue: dict | None = None) -> dict:
    return collection_outcomes(sources, catalogue or {})["counts"]


def test_the_six_answers_are_counted_apart():
    """FR-185: one number for 538 outcomes is one number too few."""
    sources = [
        _source(plan_item_id="a", outcome="succeeded", records_collected=12),
        *[
            _source(plan_item_id=f"b{i}", status="blocked", outcome="blocked")
            for i in range(192)
        ],
        *[_source(plan_item_id=f"c{i}", status="gone", outcome="gone") for i in range(308)],
        *[
            _source(plan_item_id=f"d{i}", status="failed", outcome="failed", error_count=1)
            for i in range(12)
        ],
        _source(plan_item_id="e", status="planned", last_error="not started: max_pages (FR-186)"),
    ]
    counts = _counts(sources)
    assert counts["succeeded"] == 1
    assert counts["blocked"] == 192
    assert counts["gone"] == 308
    assert counts["failed"] == 12
    assert counts["capped"] == 1
    # The point of the whole change: the number that asks something of the
    # operator is 12, not 513.
    assert counts["failed"] < counts["blocked"] + counts["gone"]


def test_a_dead_board_is_not_a_failure_and_a_refusal_is_not_either():
    """404 is the registry decaying; robots.txt is the product working (FR-182)."""
    counts = _counts(
        [
            _source(plan_item_id="a", status="gone", outcome="gone", error_count=1),
            _source(plan_item_id="b", status="blocked", outcome="blocked", error_count=1),
        ]
    )
    assert counts["failed"] == 0
    assert (counts["gone"], counts["blocked"]) == (1, 1)


def test_a_real_failure_stays_loud():
    """5xx, a crash, or a fetch that produced no record at all is a failure."""
    for state in ("failed", "rejected", "normalised_nothing", "extracted_nothing"):
        counts = _counts([_source(status="failed", outcome=state)])
        assert counts["failed"] == 1, state


def test_an_unrecognised_end_state_counts_as_a_failure():
    """Never quiet again: an answer nobody taught this code is not a success."""
    counts = _counts([_source(status="finished-somehow", outcome="who_knows")])
    assert counts["failed"] == 1
    assert counts["succeeded"] == 0


def test_done_without_records_is_not_collected():
    """The exact shape the earlier bug hid behind: done, 0 records, 0 errors."""
    counts = _counts([_source(status="done", outcome=None, records_collected=0)])
    assert counts["succeeded"] == 0
    assert counts["skipped"] == 1


def test_a_source_that_answered_and_holds_nothing_is_not_a_failure():
    """``no_matches``: read successfully, nothing to collect - a narrow query."""
    counts = _counts([_source(status="done", outcome="no_matches")])
    assert (counts["failed"], counts["succeeded"], counts["skipped"]) == (0, 0, 1)


def test_the_page_budget_is_told_apart_from_never_reached():
    """FR-186: a cap is a budget signal; an unplanned item is neither."""
    counts = _counts(
        [
            _source(
                plan_item_id="a",
                status="planned",
                outcome=None,
                last_error="not started: max_pages (FR-186)",
            ),
            _source(
                plan_item_id="b",
                status="planned",
                outcome=None,
                last_error="stopped by cap: max_pages (FR-186)",
            ),
            _source(plan_item_id="c", status="planned", outcome=None, last_error=None),
        ]
    )
    assert counts["capped"] == 2
    assert counts["pending"] == 1
    assert counts["failed"] == 0


def test_an_excluded_source_is_skipped_not_pending():
    """FR-163: the job seeker took it out of the plan; nothing is owed on it."""
    counts = _counts(
        [_source(status="planned", outcome=None, excluded_by_user=True)]
    )
    assert counts["skipped"] == 1


def test_failures_are_listed_worst_first_and_the_others_are_grouped():
    outcomes = collection_outcomes(
        [
            _source(
                plan_item_id="a",
                adapter_key="board.eures",
                status="failed",
                outcome="extracted_nothing",
                error_count=1,
                last_error="fetched 1 page(s) and extracted no record",
            ),
            _source(
                plan_item_id="b",
                adapter_key="ats.lever",
                status="failed",
                outcome="failed",
                error_count=9,
                last_error="ConnectError",
            ),
            *[
                _source(
                    plan_item_id=f"c{i}",
                    adapter_key="ats.personio",
                    status="gone",
                    outcome="gone",
                    last_error="HTTP 404",
                )
                for i in range(3)
            ],
        ],
        {},
    )
    assert [row["adapter_key"] for row in outcomes["failed"]] == ["ats.lever", "board.eures"]
    assert [(g["adapter_key"], g["count"]) for g in outcomes["gone"]] == [("ats.personio", 3)]
    # 308 dead slugs are never read one at a time, so the group carries examples
    # rather than every sentence.
    assert outcomes["gone"][0]["reasons"] == ["HTTP 404"]


def test_the_blocked_list_carries_its_ir_101_acknowledgement_state():
    """An operator defending how this product collects needs the reason and the ack."""
    outcomes = collection_outcomes(
        [
            _source(
                adapter_key="board.stepstone",
                status="blocked",
                outcome="blocked",
                last_error="terms of service prohibit automated access (IR-101)",
            )
        ],
        {
            "board.stepstone": {
                "adapter_key": "board.stepstone",
                "display_name": "StepStone",
                "tos_status": "prohibited",
                "requires_ack": 1,
                "acknowledged_at": None,
            }
        },
    )
    group = outcomes["blocked"][0]
    assert group["tos_status"] == "prohibited"
    assert group["requires_ack"] is True
    assert group["acknowledged_at"] is None
    assert group["reasons"] == ["terms of service prohibit automated access (IR-101)"]


def test_an_adapter_dead_on_every_slug_is_breakage_rather_than_decay():
    """NFR-403: 27.5% liveness is the design; 100% dead is a broken adapter."""
    dead_everywhere = [
        _source(plan_item_id=f"a{i}", adapter_key="ats.broken", status="gone", outcome="gone")
        for i in range(8)
    ]
    decaying = [
        _source(plan_item_id=f"b{i}", adapter_key="ats.lever", status="gone", outcome="gone")
        for i in range(3)
    ] + [
        _source(
            plan_item_id=f"c{i}",
            adapter_key="ats.lever",
            status="done",
            outcome="succeeded",
            records_collected=4,
        )
        for i in range(5)
    ]
    outcomes = collection_outcomes(dead_everywhere + decaying, {})
    flagged = {g["adapter_key"]: g for g in outcomes["gone"]}
    assert flagged["ats.broken"]["suspected_breakage"] is True
    assert flagged["ats.broken"]["share"] == 1.0
    # Three dead slugs out of eight tried is the registry, not the adapter.
    assert flagged["ats.lever"]["suspected_breakage"] is False
    assert flagged["ats.lever"]["attempted"] == 8


def test_a_small_sample_is_not_called_breakage():
    """Two dead slugs cannot tell a broken adapter from an unlucky pair."""
    outcomes = collection_outcomes(
        [
            _source(plan_item_id=f"a{i}", adapter_key="ats.tiny", status="gone", outcome="gone")
            for i in range(2)
        ],
        {},
    )
    assert outcomes["gone"][0]["suspected_breakage"] is False


def test_the_headline_numbers_are_the_ones_the_operator_reads():
    outcomes = collection_outcomes(
        [
            _source(plan_item_id="a", outcome="succeeded", records_collected=1200),
            _source(plan_item_id="b", outcome="succeeded", records_collected=40),
            _source(plan_item_id="c", status="gone", outcome="gone", error_count=1),
        ],
        {},
    )
    assert outcomes["records"] == 1240
    assert outcomes["producing_sources"] == 2
    # The old headline is kept rather than dropped: a retried page still cost
    # something, it is simply not the number to lead on.
    assert outcomes["error_count"] == 1


def test_a_relabelled_row_is_read_from_the_column_not_the_stale_caps_blob():
    """Migration 130's verdict beats the word the pre-fix code left in ``caps``.

    Measured on the installed database: migration 130 relabelled 504 items of
    one campaign from the evidence already in ``last_error`` - 293 robots
    refusals to ``blocked`` and 211 dead boards to ``gone`` - by rewriting
    ``status`` and the new ``outcome_state`` column.  It could not rewrite the
    ``caps.outcome.state`` blob, which still holds what the code that hid this
    bug wrote there: ``failed``.

    This endpoint read ``caps`` first, so all 504 went straight back into the
    failure count and the dashboard still said 976 failures with 0 blocked and
    0 gone - the re-classification was invisible to the operator it was for.
    """
    relabelled_blocked = _source(
        status="blocked", outcome_state="blocked", outcome="failed",
        outcome_reason="https://x.example/jobs: robots.txt disallows this source (FR-182)",
        last_error="robots.txt disallows this source (FR-182)", error_count=0,
    )
    relabelled_gone = _source(
        status="gone", outcome_state="gone", outcome="failed",
        outcome_reason="https://x.example/jobs: HTTP 404",
        last_error="SourceUnavailable: 1 request(s), 0 answered; HTTP 404", error_count=0,
    )
    counts = _counts([relabelled_blocked, relabelled_gone])
    assert counts["blocked"] == 1
    assert counts["gone"] == 1
    assert counts["failed"] == 0


def test_a_failure_is_still_a_failure_when_both_spellings_say_so():
    """The precedence must not be a way to make a real failure quiet."""
    counts = _counts([
        _source(status="failed", outcome_state="failed", outcome="failed",
                last_error="HTTP 503", error_count=3),
    ])
    assert counts["failed"] == 1


def test_an_item_settled_since_the_migration_needs_no_caps_blob_at_all():
    """The worker writes the column; ``caps`` is the older copy, not the source."""
    counts = _counts([
        _source(status="gone", outcome_state="gone", outcome=None, error_count=0),
        _source(plan_item_id="b", status="blocked", outcome_state="blocked",
                outcome=None, error_count=0),
    ])
    assert counts == {**counts, "gone": 1, "blocked": 1, "failed": 0}

