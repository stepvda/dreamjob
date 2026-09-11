"""Scoring reaches every opportunity, now and for the ones already collected (FR-281).

Two failures this file exists to prevent, both found in the live installation:

* 52,672 opportunities carried no score because they were collected before
  scoring was wired into the end of collection.  A backfill has to find them
  all, and it has to find them through the opportunities themselves - an
  earlier version built its campaign list from ``list_campaigns``, which is
  bounded, and silently skipped a campaign holding 241 of them.
* The watchlist adds a vacancy to the ranked list as it finds it.  "Ranked"
  has to mean scored, or the new row sorts to the bottom and reads as broken.

Runs against the shared scratch database; no network and no model.
"""

from __future__ import annotations

from uuid import uuid4

from dreamjob.db.connection import insert_row, query_one, utcnow
from dreamjob.db.repositories import opportunities as opp_repo
from dreamjob.db.repositories import seekers as seeker_repo
from dreamjob.pipeline import scoring


def _seeker() -> str:
    return seeker_repo.create_seeker(
        f"backfill-{uuid4().hex[:8]}@example.com", "Backfill Tester", password_hash="x"
    )


def _campaign(seeker: str, name: str = "Backfill") -> str:
    directives = insert_row(
        "directive_set",
        {"job_seeker_id": seeker, "name": f"d-{uuid4().hex[:6]}", "created_at": utcnow()},
    )
    profile = insert_row(
        "profile_version",
        {
            "job_seeker_id": seeker,
            # profile_version is unique on (job_seeker_id, version), so a second
            # campaign for the same seeker needs the next version.
            "version": (query_one(
                "SELECT COALESCE(MAX(version), 0) AS v FROM profile_version WHERE job_seeker_id = ?",
                (seeker,),
            )["v"] or 0) + 1,
            "sections": {},
            "created_at": utcnow(),
        },
    )
    return insert_row(
        "campaign",
        {
            "job_seeker_id": seeker,
            "directive_set_id": directives,
            "profile_version_id": profile,
            "name": name,
            "status": "completed",
            "created_at": utcnow(),
        },
    )


def _opportunity(seeker: str, campaign: str, title: str, *, score=None, manual_rank=None) -> str:
    return opp_repo.create_opportunity(
        seeker,
        campaign,
        {
            "kind": "vacancy",
            "title": title,
            "function_family": "software engineering",
            "seniority": "senior",
            "country": "BE",
            "work_arrangement": "hybrid",
            "language": "en",
            "score": score,
            "manual_rank": manual_rank,
        },
    )


def test_list_unscored_returns_only_rows_without_a_score():
    seeker = _seeker()
    campaign = _campaign(seeker)
    _opportunity(seeker, campaign, "Scored one", score=80.0)
    unscored_id = _opportunity(seeker, campaign, "Unscored one")

    rows = opp_repo.list_unscored(seeker, campaign)
    assert [r["id"] for r in rows] == [unscored_id]
    assert opp_repo.count_unscored(seeker, campaign) == 1


def test_score_unscored_scores_every_missing_row_and_is_idempotent():
    seeker = _seeker()
    campaign = _campaign(seeker)
    for i in range(5):
        _opportunity(seeker, campaign, f"Role {i}")

    assert scoring.score_unscored(seeker, campaign) == 5
    assert opp_repo.count_unscored(seeker, campaign) == 0
    # Running it again finds nothing to do rather than rescoring.
    assert scoring.score_unscored(seeker, campaign) == 0

    scored = query_one(
        "SELECT COUNT(*) n FROM opportunity WHERE campaign_id = ? AND score IS NOT NULL",
        (campaign,),
    )
    assert scored["n"] == 5


def test_score_unscored_leaves_a_manual_order_alone():
    """A backfill must never disturb the seeker's own ranking (FR-284)."""
    seeker = _seeker()
    campaign = _campaign(seeker)
    _opportunity(seeker, campaign, "Pinned at the top", manual_rank=1)

    scoring.score_unscored(seeker, campaign)
    row = query_one("SELECT manual_rank, score FROM opportunity WHERE campaign_id = ?", (campaign,))
    assert row["manual_rank"] == 1
    assert row["score"] is not None


def test_only_ids_narrows_the_sweep_to_the_rows_just_added():
    """The watchlist scores the vacancy it added, not a campaign's backlog."""
    seeker = _seeker()
    campaign = _campaign(seeker)
    wanted = _opportunity(seeker, campaign, "New from the watchlist")
    _opportunity(seeker, campaign, "Old backlog row")

    assert scoring.score_unscored(seeker, campaign, only_ids=[wanted]) == 1
    assert opp_repo.count_unscored(seeker, campaign) == 1  # the backlog row remains


def test_campaign_ids_with_unscored_finds_campaigns_a_bounded_list_would_miss():
    """The list comes from the opportunities, so no campaign can be skipped."""
    seeker = _seeker()
    first = _campaign(seeker, "First")
    second = _campaign(seeker, "Second")
    _opportunity(seeker, first, "A")
    _opportunity(seeker, second, "B")

    found = set(opp_repo.campaign_ids_with_unscored(seeker))
    assert found == {first, second}


def test_a_failing_row_does_not_stop_the_sweep_or_loop_for_ever(monkeypatch):
    """One row that cannot be scored is left alone, and the loop terminates.

    If every row in a page fails, the set of unscored rows never shrinks; the
    sweep has to notice and stop rather than read the same rows for ever.
    """
    seeker = _seeker()
    campaign = _campaign(seeker)
    for i in range(3):
        _opportunity(seeker, campaign, f"Unscorable {i}")

    def explode(*_args, **_kwargs):
        raise RuntimeError("cannot score this one")

    monkeypatch.setattr(scoring, "score_opportunity", explode)
    # Returns 0 rather than hanging or raising.
    assert scoring.score_unscored(seeker, campaign, page=2) == 0
    assert opp_repo.count_unscored(seeker, campaign) == 3
