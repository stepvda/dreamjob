"""Two-stage ranking: pre-rank everything, spend the model on the top N.

Requirements exercised: FR-281 (a score for every opportunity), FR-282 (the
LLM half - semantic dream fit and the written rationale), FR-284 (manual order
untouched), NFR-104 (bounded token budget, graceful degradation), NFR-305
(advisory).  Plan item N11 of docs/Data_Gathering_Plan.md: one campaign now
collects ~93,000 opportunities and one scoring call is ~10k tokens, so scoring
every row with the model would cost ~100 M tokens and the 2 M default budget
degrades after roughly two hundred.

The defect these tests pin down is not the cost, it is the silence.  The old
scorer read the first ``limit=1000`` rows of a campaign, scored them, and
reported ``scored: 1000`` with nothing to say about the 92,000 it never looked
at - a truncation that reads as "we looked at everything".  Every test below
asserts the numbers the report now has to publish.

No network and no model: a stub client stands in for the one call the scorer
makes per opportunity.
"""

from __future__ import annotations

import pytest
from dreamjob.config import get_settings
from dreamjob.db.connection import insert_row, query_one, utcnow
from dreamjob.db.migrator import migrate
from dreamjob.db.repositories import opportunities as repo
from dreamjob.llm.client import BudgetExhausted
from dreamjob.pipeline import scoring


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DREAMJOB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DREAMJOB_DB_PATH", str(tmp_path / "data" / "test.db"))
    get_settings.cache_clear()
    get_settings()
    migrate()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# A campaign with a corpus, built without the collection pipeline
# ---------------------------------------------------------------------------


class StubLLM:
    """Records one call per opportunity; raises when asked to run out of budget."""

    def __init__(self, response: dict | None = None, *, raises: Exception | None = None):
        self.response = response or {
            "dream_fit": 0.9,
            "dream_fit_reason": "Leads a data platform team.",
            "rationale": "Written by the model.",
            "strongest_match": "data platform leadership",
            "biggest_gap": "no compensation stated",
        }
        self.raises = raises
        self.calls: list[str] = []

    def complete_json(self, task, system, user, **kwargs):
        self.calls.append(str(kwargs.get("entity_id")))
        if self.raises is not None:
            raise self.raises
        return self.response


def _campaign() -> tuple[str, dict]:
    seeker_id = insert_row(
        "job_seeker",
        {
            "email": "seeker@example.com",
            "display_name": "Test Seeker",
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )
    directive_id = insert_row(
        "directive_set",
        {
            "job_seeker_id": seeker_id,
            "name": "Benelux data leadership",
            "job_content": {
                "target_titles": ["Head of Data"],
                "function_families": ["data & analytics"],
                "seniority_min": "senior",
                "seniority_max": "director",
                "must_have_skills": ["python", "sql"],
            },
            "location": {"countries": ["BE", "NL"]},
            "work_arrangement": {
                "arrangements": ["hybrid", "remote"],
                "contract_types": ["permanent"],
            },
            "created_at": utcnow(),
        },
    )
    profile_id = insert_row(
        "profile_version",
        {
            "job_seeker_id": seeker_id,
            "version": 1,
            "sections": {"experience": [{"company": "Acme NV", "title": "Data Lead"}]},
            "created_at": utcnow(),
        },
    )
    for label in ("Python", "SQL"):
        insert_row(
            "profile_skill",
            {
                "job_seeker_id": seeker_id,
                "profile_version_id": profile_id,
                "raw_label": label,
                "normalised_label": label,
                "proficiency": 5,
            },
        )
    composite_id = insert_row(
        "composite_profile",
        {
            "job_seeker_id": seeker_id,
            "profile_version_id": profile_id,
            "version": 1,
            "narrative": "Data leader in logistics.",
            "seniority": {"id": "seniority:1", "level": "manager", "text": "Data manager"},
            "core_competencies": [{"id": "core_competencies:1", "text": "Python"}],
            "domains": [{"id": "domains:1", "text": "logistics"}],
            "created_at": utcnow(),
        },
    )
    dream_id = insert_row(
        "dream_job_model",
        {
            "job_seeker_id": seeker_id,
            "version": 1,
            "statement": "Lead a data team in a growing logistics scale-up.",
            "target_roles": [{"title": "Head of Data", "priority": 1}],
            "role_families": [{"family": "data & analytics"}],
            "responsibilities": [
                {"activity": "build and lead a data platform team", "importance": "must"}
            ],
            "created_at": utcnow(),
        },
    )
    campaign_id = insert_row(
        "campaign",
        {
            "job_seeker_id": seeker_id,
            "directive_set_id": directive_id,
            "profile_version_id": profile_id,
            "composite_profile_id": composite_id,
            "dream_job_model_id": dream_id,
            "name": "Autumn campaign",
            "status": "running",
            "token_budget": 1_000_000,
            "created_at": utcnow(),
        },
    )
    return seeker_id, query_one("SELECT * FROM campaign WHERE id = ?", (campaign_id,))


def _opportunity(seeker_id: str, campaign_id: str, *, strong: bool, n: int) -> str:
    """One opportunity the arithmetic can separate from its opposite."""
    if strong:
        values = {
            "title": f"Head of Data {n}",
            "function_family": "data & analytics",
            "seniority": "manager",
            "description": (
                "You will build and lead a data platform team. Python and SQL required."
            ),
            "required_skills": ["Python", "SQL"],
            "location": "Ghent",
            "country": "BE",
            "work_arrangement": "hybrid",
            "contract_type": "permanent",
        }
    else:
        values = {
            "title": f"Warehouse operator {n}",
            "function_family": "logistics operations",
            "seniority": "junior",
            "description": "Forklift work on a night shift, no computer involved.",
            "required_skills": ["forklift"],
            "location": "Dallas",
            "country": "US",
            "work_arrangement": "onsite",
            "contract_type": "freelance",
        }
    return insert_row(
        "opportunity",
        {
            "job_seeker_id": seeker_id,
            "campaign_id": campaign_id,
            "kind": "vacancy",
            "created_at": utcnow(),
            "updated_at": utcnow(),
            **values,
        },
    )


def _corpus(seeker_id: str, campaign_id: str, strong: int, weak: int) -> tuple[set, set]:
    strong_ids = {
        _opportunity(seeker_id, campaign_id, strong=True, n=i) for i in range(strong)
    }
    weak_ids = {_opportunity(seeker_id, campaign_id, strong=False, n=i) for i in range(weak)}
    return strong_ids, weak_ids


def _rows(seeker_id: str, campaign_id: str) -> dict[str, dict]:
    return {
        row["id"]: row
        for row in repo.list_opportunities(
            seeker_id, campaign_id=campaign_id, limit=100, respect_manual_order=False
        )
    }


# ---------------------------------------------------------------------------
# N11: the cut
# ---------------------------------------------------------------------------


def test_everything_is_pre_ranked_and_only_the_top_n_reach_the_model(db):
    """The corpus is ranked in full; the model is spent on the best rows only."""
    seeker_id, campaign = _campaign()
    strong_ids, weak_ids = _corpus(seeker_id, campaign["id"], strong=2, weak=4)
    stub = StubLLM()

    report = scoring.score_campaign(campaign, llm=stub, llm_limit=2)

    coverage = report.as_dict()["coverage"]
    assert coverage["total"] == 6
    assert coverage["pre_ranked"] == 6           # nothing was left unranked
    assert coverage["llm_scored"] == 2           # the cut held
    assert coverage["llm_succeeded"] == 2
    assert coverage["deterministic_only"] == 4
    assert coverage["not_scored"] == 0
    assert coverage["truncated"] is False
    assert coverage["llm_limit"] == 2
    assert "deterministic scores only" in coverage["note"]

    # Two calls, not six: the model saw the top of the ranking and nothing else.
    assert len(stub.calls) == 2
    assert set(stub.calls) == strong_ids

    rows = _rows(seeker_id, campaign["id"])
    assert len(rows) == 6
    # Every row carries a score and its own reasons - the four that missed the
    # cut were ranked, not skipped (FR-281, FR-282).
    assert all(row["score"] is not None for row in rows.values())
    assert all(row["rationale"] for row in rows.values())
    for identifier in strong_ids:
        assert rows[identifier]["dream_fit_detail"]["method"] == "llm+deterministic"
        assert rows[identifier]["rationale"] == "Written by the model."
    for identifier in weak_ids:
        assert rows[identifier]["dream_fit_detail"]["method"] == "deterministic"
        assert rows[identifier]["score_detail"]["components"]["profile_fit"]["reasons"]


def test_the_cut_is_reproducible(db):
    """FR-281: the same corpus must choose the same rows for the model twice."""
    seeker_id, campaign = _campaign()
    _corpus(seeker_id, campaign["id"], strong=3, weak=5)

    first, second = StubLLM(), StubLLM()
    scoring.score_campaign(campaign, llm=first, llm_limit=3)
    scoring.score_campaign(campaign, llm=second, llm_limit=3)

    assert first.calls == second.calls
    assert len(first.calls) == 3


def test_a_capped_run_says_which_rows_it_never_looked_at(db):
    """The regression: a truncated run used to report plain success.

    ``scored: 2`` on a corpus of five reads as "we looked at everything".  The
    report now has to publish ``total``, ``not_scored`` and ``truncated``, and
    the unscored rows are visibly unscored in the database.
    """
    seeker_id, campaign = _campaign()
    _corpus(seeker_id, campaign["id"], strong=2, weak=3)

    report = scoring.score_campaign(campaign, use_llm=False, limit=2)

    coverage = report.as_dict()["coverage"]
    assert coverage["total"] == 5
    assert coverage["pre_ranked"] == 2
    assert coverage["not_scored"] == 3
    assert coverage["truncated"] is True
    assert coverage["prerank_limit"] == 2
    assert "not looked at" in coverage["note"]

    unscored = [r for r in _rows(seeker_id, campaign["id"]).values() if r["score"] is None]
    assert len(unscored) == 3          # the cap is real, and the report admits it


def test_an_uncapped_run_covers_the_whole_corpus(db):
    """The pre-rank costs no tokens, so by default it has no cap at all."""
    seeker_id, campaign = _campaign()
    _corpus(seeker_id, campaign["id"], strong=2, weak=3)

    report = scoring.score_campaign(campaign, use_llm=False)

    coverage = report.as_dict()["coverage"]
    assert coverage["prerank_limit"] is None
    assert coverage["total"] == coverage["pre_ranked"] == 5
    assert coverage["truncated"] is False
    assert coverage["llm_limit"] == 0
    assert "no model was used" in coverage["note"]
    assert all(row["score"] is not None for row in _rows(seeker_id, campaign["id"]).values())


def test_a_corpus_under_the_limit_is_scored_exactly_as_before(db):
    """Below the cut there is no cut: every row gets its one call (FR-282)."""
    seeker_id, campaign = _campaign()
    strong_ids, weak_ids = _corpus(seeker_id, campaign["id"], strong=1, weak=2)
    stub = StubLLM()

    report = scoring.score_campaign(campaign, llm=stub, llm_limit=500)

    coverage = report.as_dict()["coverage"]
    assert coverage["total"] == coverage["pre_ranked"] == coverage["llm_scored"] == 3
    assert coverage["deterministic_only"] == 0
    assert coverage["truncated"] is False
    assert set(stub.calls) == strong_ids | weak_ids
    assert report.scored == 3 and report.with_llm == 3


def test_the_default_cut_comes_from_the_setting(db, monkeypatch):
    """NFR-104: DREAMJOB_LLM_SCORED_LIMIT is the knob, and it is honoured."""
    seeker_id, campaign = _campaign()
    _corpus(seeker_id, campaign["id"], strong=2, weak=2)
    monkeypatch.setenv("DREAMJOB_LLM_SCORED_LIMIT", "1")
    get_settings.cache_clear()
    assert get_settings().llm_scored_limit == 1

    stub = StubLLM()
    report = scoring.score_campaign(campaign, llm=stub)

    assert report.llm_limit == 1
    assert len(stub.calls) == 1
    assert report.as_dict()["coverage"]["deterministic_only"] == 3


def test_a_budget_exhausted_call_is_reported_as_a_failure_not_a_success(db):
    """NFR-104: degradation is visible - the row keeps its deterministic score.

    A call that dies on the token budget used to leave ``scored`` claiming the
    row was scored with no way to tell the model never answered.
    """
    seeker_id, campaign = _campaign()
    _corpus(seeker_id, campaign["id"], strong=2, weak=2)
    stub = StubLLM(raises=BudgetExhausted("campaign token budget exhausted"))

    report = scoring.score_campaign(campaign, llm=stub, llm_limit=2)

    coverage = report.as_dict()["coverage"]
    assert coverage["llm_scored"] == 2
    assert coverage["llm_succeeded"] == 0
    assert coverage["llm_failed"] == 2
    assert "ran out of token budget" in coverage["note"]
    rows = _rows(seeker_id, campaign["id"])
    assert all(row["score"] is not None for row in rows.values())
    assert all(row["dream_fit_detail"]["method"] == "deterministic" for row in rows.values())


def test_manual_order_survives_a_two_stage_run(db):
    """FR-284: the pre-rank writes scores; it never touches a manual position."""
    seeker_id, campaign = _campaign()
    strong_ids, _weak = _corpus(seeker_id, campaign["id"], strong=2, weak=3)
    pinned = sorted(strong_ids)[0]
    repo.update_opportunity(pinned, {"manual_rank": 1}, job_seeker_id=seeker_id)

    report = scoring.score_campaign(campaign, use_llm=False)

    assert report.manual_ranks_preserved == 1
    assert _rows(seeker_id, campaign["id"])[pinned]["manual_rank"] == 1


def test_the_pre_rank_pages_through_the_corpus_without_losing_rows(db, monkeypatch):
    """Pages cannot be ordered by the column the loop is rewriting.

    The pre-rank writes ``score`` on every row it reads, and ``score`` is what
    the ranked list is sorted by; a page walk over that ordering would let rows
    slide between pages - read twice, or never read.  Twelve rows, five to a
    page: each one is read and saved exactly once.
    """
    seeker_id, campaign = _campaign()
    _corpus(seeker_id, campaign["id"], strong=6, weak=6)
    monkeypatch.setattr(scoring, "PRERANK_PAGE_SIZE", 5)
    saved: list[str] = []
    real = repo.save_scores

    def spy(opportunity_id, columns, **kwargs):
        saved.append(opportunity_id)
        real(opportunity_id, columns, **kwargs)

    monkeypatch.setattr(repo, "save_scores", spy)

    report = scoring.score_campaign(campaign, use_llm=False)

    assert report.pre_ranked == 12
    assert len(saved) == 12
    assert sorted(saved) == sorted(set(saved))          # no row scored twice
    rows = _rows(seeker_id, campaign["id"])
    assert len(rows) == 12
    assert all(row["score"] is not None for row in rows.values())
