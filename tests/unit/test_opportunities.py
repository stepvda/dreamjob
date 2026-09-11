"""Opportunity synthesis, speculative openings, compensation and scoring.

Requirements exercised: FR-261..265, FR-281..285, FR-383, NFR-104, NFR-305,
CR-405.

Everything runs against a throw-away SQLite file with no network.  The two
steps that would normally call DeepSeek - speculative openings (FR-262) and the
semantic dream-fit plus rationale (FR-282) - are driven by a stub client that
implements the two methods the pipeline actually uses, so the JSON contract of
each prompt is exercised without egress.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from dreamjob.config import get_settings
from dreamjob.db.connection import insert_row, query_one, update_row, utcnow
from dreamjob.db.migrator import migrate
from dreamjob.db.repositories import opportunities as repo
from dreamjob.pipeline import compensation as comp_mod
from dreamjob.pipeline import opportunities as synth
from dreamjob.pipeline import scoring, speculative


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DREAMJOB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DREAMJOB_DB_PATH", str(tmp_path / "data" / "test.db"))
    get_settings.cache_clear()
    get_settings()
    migrate()
    yield
    get_settings.cache_clear()


def _iso(days_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Fixtures for one small, complete campaign
# ---------------------------------------------------------------------------


class StubLLM:
    """The two members of ``LLMClient`` the pipeline touches, and nothing else."""

    class Budget:
        def __init__(self, degrade: bool = False) -> None:
            self._degrade = degrade

        def should_degrade(self) -> bool:
            return self._degrade

    def __init__(self, response, *, degrade: bool = False) -> None:
        self.response = response
        self.budget = StubLLM.Budget(degrade)
        self.calls: list[dict] = []

    def complete_json(self, task, system, user, **kwargs):
        self.calls.append({"task": task, "kwargs": kwargs})
        if callable(self.response):
            return self.response(self.calls[-1])
        return self.response


def _seeker() -> str:
    return insert_row(
        "job_seeker",
        {
            "email": "seeker@example.com",
            "display_name": "Test Seeker",
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )


def _company(name: str, **overrides) -> str:
    values = {
        "normalised_name": name.lower(),
        "name": name,
        "country": "BE",
        "business_summary": "Industrial logistics software for European ports.",
        "sector_codes": ["62010"],
        "size_fte": 180,
        "size_band": "b50_250",
        "stage": "scaleup",
        "ownership": "founder_led",
        "trajectory": "growing",
        "structure": {"departments": ["engineering", "data", "operations"]},
        "collected_at": utcnow(),
        "confidence": 0.8,
    }
    values.update(overrides)
    return insert_row("company", values)


def _campaign(seeker_id: str) -> dict:
    directive_id = insert_row(
        "directive_set",
        {
            "job_seeker_id": seeker_id,
            "name": "Benelux data leadership",
            "job_content": {
                "target_titles": ["Head of Data", "Data Engineering Manager"],
                "function_families": ["data & analytics"],
                "seniority_min": "senior",
                "seniority_max": "director",
                "must_have_skills": ["python", "sql"],
                "keywords_to_avoid": ["door-to-door sales"],
            },
            "company_type": {"size_bands": ["b50_250"], "stages": ["scaleup"]},
            "location": {"countries": ["BE", "NL"]},
            "work_arrangement": {
                "arrangements": ["hybrid", "remote"],
                "contract_types": ["permanent"],
            },
            "compensation": {"minimum_package": 85_000, "currency": "EUR", "period": "annual"},
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
    insert_row(
        "profile_skill",
        {
            "job_seeker_id": seeker_id,
            "profile_version_id": profile_id,
            "raw_label": "Python",
            "normalised_label": "Python",
            "proficiency": 5,
        },
    )
    insert_row(
        "profile_skill",
        {
            "job_seeker_id": seeker_id,
            "profile_version_id": profile_id,
            "raw_label": "SQL",
            "normalised_label": "SQL",
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
            "statement": "Lead a data team in a growing logistics scale-up, mostly remote.",
            "target_roles": [{"title": "Head of Data", "priority": 1}],
            "role_families": [{"family": "data & analytics"}],
            "responsibilities": [
                {"activity": "build and lead a data platform team", "importance": "must"}
            ],
            "company_characteristics": [
                {"attribute": "stage", "value": "scaleup", "importance": "strong"}
            ],
            "deal_breakers": [{"constraint": "door-to-door sales", "hard": True}],
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
    insert_row(
        "consent_record",
        {
            "job_seeker_id": seeker_id,
            "kind": "llm_transfer",
            "granted": 1,
            "granted_at": utcnow(),
        },
    )
    return query_one("SELECT * FROM campaign WHERE id = ?", (campaign_id,))


def _campaign_company(campaign_id: str, company_id: str) -> None:
    """Tie a company to this campaign the way collection does: through provenance."""
    plan_item_id = insert_row(
        "source_plan_item",
        {
            "campaign_id": campaign_id,
            "adapter_key": "website.crawl",
            "status": "done",
            "created_at": utcnow(),
        },
    )
    insert_row(
        "provenance",
        {
            "entity_type": "company",
            "entity_id": company_id,
            "source_plan_item_id": plan_item_id,
            "adapter_key": "website.crawl",
            "created_at": utcnow(),
        },
    )


def _collected_vacancy(campaign_id: str, company_id: str, **overrides) -> str:
    """A vacancy plus the provenance that ties it to this campaign's plan."""
    plan_item_id = insert_row(
        "source_plan_item",
        {
            "campaign_id": campaign_id,
            "adapter_key": "jobboard.test",
            "status": "done",
            "created_at": utcnow(),
        },
    )
    values = {
        "company_id": company_id,
        "title": "Senior Data Engineering Manager",
        "description": (
            "You will build and lead a data platform team. Python and SQL are required. "
            "We work hybrid, 2 days a week from home, on a permanent contract. "
            "Apply via careers@example.com."
        ),
        "required_skills": ["Python", "SQL"],
        "desirable_skills": ["dbt"],
        "location": "Ghent",
        "country": "BE",
        "posted_at": _iso(5),
        "source_url": "https://example.com/jobs/1",
        "source_adapter": "jobboard.test",
        "collected_at": _iso(1),
    }
    values.update(overrides)
    vacancy_id = insert_row("vacancy", values)
    insert_row(
        "provenance",
        {
            "entity_type": "vacancy",
            "entity_id": vacancy_id,
            "source_plan_item_id": plan_item_id,
            "adapter_key": "jobboard.test",
            "created_at": utcnow(),
        },
    )
    return vacancy_id


# ---------------------------------------------------------------------------
# FR-261: normalisation
# ---------------------------------------------------------------------------


def test_normalise_vacancy_fills_the_fr261_field_set(db):
    company_id = _company("Portex NV")
    vacancy = {
        "id": "v1",
        "company_id": company_id,
        "title": "Senior Data Engineering Manager",
        "description": (
            "Permanent contract, hybrid working with 2 days a week from home. "
            "Send your application to jobs@portex.example."
        ),
        "required_skills": ["Python", "SQL"],
        "location": "Ghent",
        "country": "BE",
        "posted_at": _iso(3),
        "source_url": "https://portex.example/vacancies/12",
        "source_adapter": "website.crawl",
    }
    record = synth.normalise_vacancy(vacancy, {"id": company_id, "country": "BE"})

    assert record["kind"] == synth.KIND_VACANCY
    assert record["function_family"] == "data & analytics"
    assert record["seniority"] == "manager"
    assert record["work_arrangement"] == "hybrid"
    assert record["remote_days"] == 2
    assert record["contract_type"] == "permanent"
    assert record["application_channel"] == "email"
    assert record["application_target"] == "jobs@portex.example"
    assert record["source_url"] == "https://portex.example/vacancies/12"
    assert "Python" in record["required_skills"]
    assert record["comp_is_stated"] == 0


def test_synthesis_is_idempotent_and_respects_directives(db):
    seeker_id = _seeker()
    campaign = _campaign(seeker_id)
    company_id = _company("Portex NV")
    _collected_vacancy(campaign["id"], company_id)
    # Excluded by the keywords_to_avoid directive.
    _collected_vacancy(
        campaign["id"], company_id, title="Door-to-door sales representative",
        description="Door-to-door sales across Flanders.", source_url="https://x/2",
    )
    # Excluded by the location directive.
    _collected_vacancy(
        campaign["id"], company_id, title="Data Manager", country="ES",
        source_url="https://x/3",
    )

    first = synth.synthesise_campaign(campaign)
    assert first.created == 1
    assert first.rejected == 2
    assert "outside_location_directives" in first.rejections

    second = synth.synthesise_campaign(campaign)
    assert second.created == 0
    assert second.refreshed == 1

    rows = repo.list_opportunities(seeker_id, campaign_id=campaign["id"])
    assert len(rows) == 1
    assert rows[0]["kind"] == "vacancy"
    assert rows[0]["required_skills"]


def test_synthesis_stops_at_the_campaign_opportunity_cap(db):
    """FR-186: one campaign's 48,269 rows are a corpus, not a shortlist."""
    from dreamjob.db.connection import to_json, update_row

    seeker_id = _seeker()
    campaign = _campaign(seeker_id)
    update_row("campaign", campaign["id"], {"caps": to_json({"max_opportunities": 1})})
    campaign = query_one("SELECT * FROM campaign WHERE id = ?", (campaign["id"],))
    company_id = _company("Portex NV")
    for n in range(3):
        _collected_vacancy(campaign["id"], company_id, source_url=f"https://x/cap-{n}")

    report = synth.synthesise_campaign(campaign)
    assert report.created == 1
    assert report.dropped_over_cap == 2
    assert report.as_dict()["capped"] is True
    assert len(repo.list_opportunities(seeker_id, campaign_id=campaign["id"])) == 1


# ---------------------------------------------------------------------------
# FR-263 / CR-405: speculative openings are never presented as vacancies
# ---------------------------------------------------------------------------


def test_speculative_labelling_and_false_vacancy_detection():
    assert speculative.kind_label("speculative").startswith("Speculative opening")
    assert "not advertised" in speculative.disclosure_note("speculative")
    assert speculative.disclosure_note("vacancy", "nl").startswith("Deze functie is door")

    bad = "I am writing to apply for the vacancy of Head of Data you advertised last week."
    assert speculative.false_vacancy_claims(bad)
    with pytest.raises(speculative.SpeculativeClaimError):
        speculative.check_generated_material(bad, "speculative")
    # The same sentence is simply true for a real posting.
    assert speculative.check_generated_material(bad, "vacancy") == []

    good = (
        "Reading about your new Antwerp terminal, I suspect you will soon need someone "
        "to own the data platform. This role is not advertised; I am writing speculatively."
    )
    assert speculative.check_generated_material(good, "speculative") == []


def test_speculative_openings_stubbed_llm(db):
    """FR-262: openings for a company with no matching vacancy."""
    seeker_id = _seeker()
    campaign = _campaign(seeker_id)
    company_id = _company("Havenlink BV")
    _campaign_company(campaign["id"], company_id)
    insert_row(
        "hiring_signal",
        {
            "company_id": company_id,
            "signal_type": "funding",
            "description": "Series B for European expansion",
            "occurred_at": _iso(30),
            "strength": 0.9,
            "collected_at": utcnow(),
        },
    )

    stub = StubLLM(
        {
            "openings": [
                {
                    "title": "Head of Data Platform",
                    "function_family": "data & analytics",
                    "seniority": "director",
                    "description": "Would own the data platform created after the Series B.",
                    "rationale": "A Series B for expansion with no data leadership in the map.",
                    "evidence": ["Series B for European expansion"],
                    "plausibility": 0.72,
                    "plausibility_basis": "signal",
                },
                {"title": "Too vague", "plausibility": 0.05, "rationale": "none"},
            ],
            "company_read": "Funded and expanding.",
            "insufficient_evidence": False,
        }
    )

    report = speculative.generate_campaign(campaign, llm=stub, language="en")
    assert report.created == 1
    assert report.companies_generated == 1

    rows = repo.list_opportunities(seeker_id, campaign_id=campaign["id"])
    assert [r["kind"] for r in rows] == ["speculative"]
    assert rows[0]["plausibility"] == pytest.approx(0.72)
    assert rows[0]["application_channel"] == "speculative"
    assert speculative.presentation(rows[0])["is_speculative"] is True

    # NFR-205: the company material went in as untrusted blocks, not as instructions.
    assert set(stub.calls[0]["kwargs"]["untrusted"]) >= {"company", "signals", "competitors"}


def test_budget_degradation_skips_low_ranked_companies_first(db):
    """NFR-104: the named degradation - low-ranked companies lose their call."""
    seeker_id = _seeker()
    campaign = _campaign(seeker_id)
    strong = _company("Strong NV", trajectory="growing")
    weak = _company("Weak NV", trajectory="declining", normalised_name="weak nv")
    insert_row(
        "financial_analysis",
        {
            "company_id": strong,
            "trajectory": "growing",
            "ability_to_pay": 90,
            "investment_capacity": 85,
            "computed_at": utcnow(),
        },
    )
    insert_row(
        "financial_analysis",
        {
            "company_id": weak,
            "trajectory": "declining",
            "ability_to_pay": 10,
            "investment_capacity": 5,
            "computed_at": utcnow(),
        },
    )
    for company_id in (strong, weak):
        insert_row(
            "watchlist_entry",
            {
                "job_seeker_id": seeker_id,
                "company_id": company_id,
                "active": 1,
                "created_at": utcnow(),
            },
        )

    ordered = speculative.candidate_companies(campaign)
    assert [c["id"] for c in ordered] == [strong, weak]

    stub = StubLLM(
        lambda call: {
            "openings": [
                {
                    "title": "Head of Data",
                    "rationale": "Growing and funded.",
                    "plausibility": 0.6,
                }
            ]
        },
        degrade=True,
    )
    report = speculative.generate_campaign(campaign, llm=stub)
    assert report.degraded is True
    assert report.companies_generated == 1
    assert report.companies_skipped_budget == 1
    assert report.skipped_companies[0]["company_id"] == weak


# ---------------------------------------------------------------------------
# FR-264 / FR-265: compensation
# ---------------------------------------------------------------------------


def test_compensation_prefers_posted_ranges_and_reports_sources(db):
    company_id = _company("Portex NV")
    for index in range(4):
        insert_row(
            "vacancy",
            {
                "company_id": _company(f"Peer {index}"),
                "title": "Data Engineering Manager",
                "function_family": "data & analytics",
                "seniority": "director",
                "country": "BE",
                "salary_min": 90_000 + index * 1_000,
                "salary_max": 115_000 + index * 1_000,
                "salary_currency": "EUR",
                "collected_at": utcnow(),
            },
        )
    insert_row(
        "employer_review_summary",
        {
            "company_id": company_id,
            "source": "glassdoor",
            "rating": 4.0,
            "review_count": 120,
            "themes": [{"theme": "good managers", "polarity": "positive"}],
            "collected_at": utcnow(),
        },
    )

    opportunity = {
        "id": "o1",
        "company_id": company_id,
        "vacancy_id": None,
        "title": "Data Engineering Manager",
        "function_family": "data & analytics",
        "seniority": "director",
        "country": "BE",
        "comp_is_stated": 0,
    }
    estimate = comp_mod.estimate(opportunity)
    assert estimate.comp_min == pytest.approx(91_500, abs=1_000)
    assert estimate.comp_max == pytest.approx(116_500, abs=1_000)
    assert "posted_vacancies" in estimate.method
    assert estimate.confidence > 0.5
    # FR-265: advisory only - it is reported, and it does not move the range.
    assert estimate.employer_rating == 4.0
    assert any("advisory" in n for n in estimate.notes)

    stated = comp_mod.estimate(
        {**opportunity, "comp_is_stated": 1, "comp_min": 80_000, "comp_max": 95_000,
         "comp_currency": "EUR"}
    )
    assert stated.is_stated is True
    assert (stated.comp_min, stated.comp_max) == (80_000, 95_000)
    assert [s for s in stated.sources if s.kind == "stated"]
    assert all(not s.used for s in stated.sources if s.kind != "stated")


def test_company_posted_ranges_only_compare_comparable_roles(db):
    """FR-264/CR-405: an employer's other postings are not a market figure."""
    company_id = _company("Portex NV")
    for index in range(4):
        insert_row(
            "vacancy",
            {
                "company_id": company_id,
                "title": f"Warehouse Operator {index}",
                "function_family": "operations & supply chain",
                "seniority": "junior",
                "country": "BE",
                "salary_min": 28_000 + index * 100,
                "salary_max": 34_000 + index * 100,
                "salary_currency": "EUR",
                "collected_at": utcnow(),
            },
        )
    estimate = comp_mod.estimate(
        {
            "id": "o1",
            "company_id": company_id,
            "vacancy_id": None,
            "title": "Head of Data",
            "function_family": "data & analytics",
            "seniority": "director",
            "country": "BE",
            "comp_is_stated": 0,
        }
    )
    assert "company_posted" not in estimate.method
    assert estimate.comp_min > 40_000        # not the warehouse band
    assert estimate.method == "blended:builtin_prior"


def test_builtin_prior_is_the_last_resort_and_says_so(db):
    prior = comp_mod.builtin_prior(
        {"seniority": "director", "function_family": "data & analytics", "country": "BE"},
        currency="EUR",
    )
    assert prior is not None
    assert prior.kind == "builtin_prior"
    assert "not survey data" in (prior.note or "")
    assert prior.low < prior.high


def test_compensation_fit_against_the_minimum_package(db):
    seeker_id = _seeker()
    campaign = _campaign(seeker_id)
    ctx = scoring.build_context(campaign)
    clears = comp_mod.CompensationEstimate(
        currency="EUR", comp_min=95_000, comp_max=120_000, confidence=0.8
    )
    short = comp_mod.CompensationEstimate(
        currency="EUR", comp_min=45_000, comp_max=55_000, confidence=0.8
    )
    assert comp_mod.fit_against_directives(clears, ctx.directives)["score"] == 1.0
    assert comp_mod.fit_against_directives(short, ctx.directives)["score"] < 0.4


def test_compensation_is_scored_against_the_market_when_no_floor_is_stated(db):
    """FR-281: a tenth of every score was unassessed without a stated minimum.

    FR-146 never guesses a salary floor, so a directive set with
    ``minimum_package = null`` made the compensation component permanently
    null.  Given the opportunity, it is scored against the market band instead.
    """
    from dreamjob.pipeline import directives as dir_mod

    opportunity = {
        "seniority": "senior",
        "function_family": "software engineering",
        "country": "BE",
    }
    estimate = comp_mod.CompensationEstimate(
        currency="EUR", comp_min=70_000, comp_max=90_000, confidence=0.6
    )
    no_floor = dir_mod.DirectiveSetPayload()

    scored = comp_mod.fit_against_directives(estimate, no_floor, opportunity=opportunity)
    assert scored["score"] is not None
    assert scored["benchmark"] == "builtin_prior"

    # Without the opportunity there is nothing to compare the range against.
    assert comp_mod.fit_against_directives(estimate, no_floor)["score"] is None


# ---------------------------------------------------------------------------
# FR-281..283, FR-383: scoring
# ---------------------------------------------------------------------------


def _scored_campaign(db_marker=None):
    seeker_id = _seeker()
    campaign = _campaign(seeker_id)
    company_id = _company("Portex NV")
    insert_row(
        "financial_analysis",
        {
            "company_id": company_id,
            "trajectory": "growing",
            "ability_to_pay": 80,
            "investment_capacity": 70,
            "computed_at": utcnow(),
        },
    )
    _collected_vacancy(campaign["id"], company_id)
    synth.synthesise_campaign(campaign)
    return seeker_id, campaign, company_id


def test_deterministic_scoring_is_reproducible_and_explainable(db):
    seeker_id, campaign, _company_id = _scored_campaign()

    first = scoring.score_campaign(campaign, use_llm=False)
    assert first.scored == 1
    row = repo.list_opportunities(seeker_id, campaign_id=campaign["id"])[0]
    assert 0 <= row["score"] <= 100
    assert row["score_profile_fit"] is not None
    assert row["score_directive_fit"] is not None
    assert row["rationale"]
    detail = row["score_detail"]
    assert set(detail["components"]) == set(scoring.COMPONENTS)
    assert "advisory" in detail

    scoring.score_campaign(campaign, use_llm=False)
    again = repo.list_opportunities(seeker_id, campaign_id=campaign["id"])[0]
    assert again["score"] == row["score"]        # reproducible on unchanged data


def test_dream_fit_meter_is_separate_from_the_score(db):
    seeker_id, campaign, _company_id = _scored_campaign()
    scoring.score_campaign(campaign, use_llm=False)
    row = repo.list_opportunities(seeker_id, campaign_id=campaign["id"])[0]

    meter = row["dream_fit_detail"]
    assert set(meter) >= {"met", "partially_met", "violated", "unknown", "counts", "score"}
    assert meter["score"] != row["score"]         # FR-383: distinct from the overall score
    criteria = meter["met"] + meter["partially_met"] + meter["violated"] + meter["unknown"]
    assert any(c["id"].startswith("target_role") for c in criteria)
    assert all(c["explanation"] for c in criteria)
    # CR-405: a deal-breaker nothing evidences is unknown, never violated.
    deal = [c for c in criteria if c["id"].startswith("deal_breaker")]
    assert deal and deal[0]["status"] == "unknown"


def test_dream_fit_meter_lists_the_culture_criteria(db):
    """FR-383: the values half of the dream job model reaches the meter too."""
    seeker_id, campaign, company_id = _scored_campaign()
    update_row(
        "company",
        company_id,
        {
            "values_culture": {
                "stated_values": [{"value": "ownership and autonomy"}],
                "working_style": ["small autonomous squads"],
            }
        },
    )
    update_row(
        "dream_job_model",
        campaign["dream_job_model_id"],
        {
            "culture_values": [
                {"cue": "autonomy", "polarity": "seek"},
                {"cue": "micromanagement", "polarity": "avoid"},
            ]
        },
    )
    ctx = scoring.build_context(campaign)
    opportunity = repo.list_opportunities(seeker_id, campaign_id=campaign["id"])[0]
    criteria = scoring.dream_criteria(opportunity, ctx)

    culture = {c["id"]: c for c in criteria if c["id"].startswith("culture_value")}
    assert len(culture) == 2
    assert culture["culture_value:1"]["status"] == "met"
    # CR-405: a cue the company has never written down is unknown, not violated.
    assert culture["culture_value:2"]["status"] == "unknown"
    assert all(c["explanation"] for c in culture.values())


def test_score_rationale_stubbed_llm(db):
    """FR-282: the LLM supplies the semantic dream fit and the paragraph."""
    seeker_id, campaign, _company_id = _scored_campaign()
    ctx = scoring.build_context(campaign)
    opportunity = repo.list_opportunities(seeker_id, campaign_id=campaign["id"])[0]

    stub = StubLLM(
        {
            "dream_fit": 0.9,
            "dream_fit_reason": "Leading a data platform team in a logistics scale-up.",
            "criteria": [
                {
                    "id": "responsibility:1",
                    "status": "met",
                    "explanation": "The posting says build and lead a data platform team.",
                }
            ],
            "rationale": "This role would put the seeker where they said they want to be.",
            "strongest_match": "data platform leadership",
            "biggest_gap": "no compensation stated",
        }
    )
    columns = scoring.score_opportunity(opportunity, ctx, llm=stub)
    assert columns["rationale"].startswith("This role would put")
    assert columns["score_dream_fit"] > 0
    assert columns["dream_fit_detail"]["method"] == "llm+deterministic"
    refined = [
        c for c in columns["dream_fit_detail"]["met"] if c["id"] == "responsibility:1"
    ]
    assert refined and refined[0]["refined_by"] == "llm"
    assert columns["score_detail"]["strongest_match"] == "data platform leadership"


def test_weights_are_configurable_and_change_the_score(db):
    seeker_id, campaign, _company_id = _scored_campaign()
    scoring.score_campaign(campaign, use_llm=False)
    baseline = repo.list_opportunities(seeker_id, campaign_id=campaign["id"])[0]["score"]

    scoring.save_weights(seeker_id, {"reachability": 5.0}, {"source": "test"})
    scoring.score_campaign(campaign, use_llm=False)
    tilted = repo.list_opportunities(seeker_id, campaign_id=campaign["id"])[0]["score"]
    assert tilted != baseline
    assert sum(scoring.load_weights(seeker_id).values()) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# FR-284: the job seeker's own order
# ---------------------------------------------------------------------------


def test_manual_order_overrides_the_score_and_survives_recalculation(db):
    seeker_id, campaign, company_id = _scored_campaign()
    _collected_vacancy(
        campaign["id"], company_id, title="Data Platform Lead",
        source_url="https://example.com/jobs/2",
    )
    _collected_vacancy(
        campaign["id"], company_id, title="Analytics Engineer",
        source_url="https://example.com/jobs/3",
    )
    synth.synthesise_campaign(campaign)
    scoring.score_campaign(campaign, use_llm=False)

    by_score = repo.list_opportunities(seeker_id, campaign_id=campaign["id"])
    assert len(by_score) == 3
    last = by_score[-1]

    repo.set_manual_order(seeker_id, campaign["id"], [last["id"]])
    reordered = repo.list_opportunities(seeker_id, campaign_id=campaign["id"])
    assert reordered[0]["id"] == last["id"]

    report = scoring.score_campaign(campaign, use_llm=False)
    assert report.manual_ranks_preserved == 1
    after = repo.list_opportunities(seeker_id, campaign_id=campaign["id"])
    assert after[0]["id"] == last["id"]          # FR-284 acceptance criterion
    assert after[0]["manual_rank"] == 1

    repo.clear_manual_order(seeker_id, campaign["id"])
    assert repo.list_opportunities(seeker_id, campaign_id=campaign["id"])[0]["id"] != last["id"]


def test_user_controls_are_written_only_on_the_owner_row(db):
    """FR-101: the owner is part of the WHERE clause, not of a caller's promise."""
    seeker_id, campaign, _company_id = _scored_campaign()
    stranger_id = insert_row(
        "job_seeker",
        {
            "email": "stranger@example.com",
            "display_name": "Stranger",
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )
    opportunity = repo.list_opportunities(seeker_id, campaign_id=campaign["id"])[0]

    assert repo.set_user_controls(opportunity["id"], stranger_id, {"pinned": True}) is None
    assert query_one(
        "SELECT pinned FROM opportunity WHERE id = ?", (opportunity["id"],)
    )["pinned"] == 0
    assert repo.set_user_controls(opportunity["id"], seeker_id, {"pinned": True})["pinned"] is True


def test_user_controls_and_tags_survive_resynthesis(db):
    seeker_id, campaign, _company_id = _scored_campaign()
    opportunity = repo.list_opportunities(seeker_id, campaign_id=campaign["id"])[0]
    repo.set_user_controls(
        opportunity["id"],
        seeker_id,
        {"pinned": True, "selected": True, "tags": ["destination"], "user_status": "interested"},
    )
    synth.synthesise_campaign(campaign)
    scoring.score_campaign(campaign, use_llm=False)

    kept = repo.get_opportunity(opportunity["id"], seeker_id)
    assert kept["pinned"] is True
    assert kept["selected"] is True
    assert kept["tags"] == ["destination"]
    assert kept["user_status"] == "interested"


# ---------------------------------------------------------------------------
# FR-285: learning from feedback
# ---------------------------------------------------------------------------


def test_learn_weights_needs_evidence_then_moves_them(db):
    seeker_id, campaign, company_id = _scored_campaign()
    for index in range(6):
        _collected_vacancy(
            campaign["id"], company_id, title=f"Data Role {index}",
            source_url=f"https://example.com/jobs/1{index}",
        )
    synth.synthesise_campaign(campaign)
    scoring.score_campaign(campaign, use_llm=False)
    rows = repo.list_opportunities(seeker_id, campaign_id=campaign["id"], limit=20)

    thin = scoring.learn_weights(seeker_id)
    assert thin["applied"] is False
    assert "at least" in thin["reason"]

    for row in rows[:3]:
        repo.set_user_controls(row["id"], seeker_id, {"pinned": True})
        repo.save_scores(row["id"], {"score_reachability": 90.0})
    for row in rows[3:6]:
        repo.set_user_controls(
            row["id"],
            seeker_id,
            {"user_status": "not_interested", "not_interested_reason": "salary far too low"},
        )
        repo.save_scores(row["id"], {"score_reachability": 10.0})

    learned = scoring.learn_weights(seeker_id)
    assert learned["applied"] is True
    assert learned["weights"]["reachability"] > scoring.DEFAULT_WEIGHTS["reachability"]
    assert learned["learned_from"]["separations"]["reachability"]["separation"] > 0

    suggestions = scoring.suggest_directive_refinements(seeker_id)
    assert any(s["topic"] == "compensation" for s in suggestions)
    assert all(0 < s["confidence"] <= 0.9 for s in suggestions)


# ---------------------------------------------------------------------------
# The API surface (FR-283, FR-284, FR-263)
# ---------------------------------------------------------------------------


@pytest.fixture()
def api(db):
    """Only this slice's router, with authentication stubbed out."""
    from dreamjob.api.deps import CurrentSeeker, current_seeker
    from dreamjob.api.routers import opportunities as router_module
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    seeker_id, campaign, company_id = _scored_campaign()
    scoring.score_campaign(campaign, use_llm=False)

    app = FastAPI()
    app.include_router(router_module.router, prefix="/api/opportunities")
    app.dependency_overrides[current_seeker] = lambda: CurrentSeeker(
        id=seeker_id, email="seeker@example.com", display_name="S", is_admin=False, locale="en"
    )
    with TestClient(app) as client:
        yield client, seeker_id, campaign, company_id


def test_api_ranked_list_labels_every_opportunity(api):
    client, _seeker_id, campaign, _company_id = api
    response = client.get("/api/opportunities", params={"campaign_id": campaign["id"]})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    item = body["items"][0]
    # FR-263: the distinction travels with every response.
    assert item["kind"] == "vacancy"
    assert item["is_speculative"] is False
    assert item["kind_label"] == "Advertised vacancy"
    assert item["disclosure_note"]
    # FR-283: links to the company profile and the source vacancy.
    assert item["links"]["company_profile"].startswith("/api/companies/")
    assert item["links"]["source_vacancy"]
    assert "advisory" in body      # NFR-305

    # The knowledge-base link must resolve, not just look like a URL.
    linked = client.get(item["links"]["knowledge_base_vacancy"])
    assert linked.status_code == 200
    assert linked.json()["id"] == item["vacancy_id"]


def test_api_user_controls_reorder_and_weights(api):
    client, seeker_id, campaign, company_id = api
    _collected_vacancy(
        campaign["id"], company_id, title="Data Platform Lead",
        source_url="https://example.com/jobs/9",
    )
    synth.synthesise_campaign(campaign)
    scoring.score_campaign(campaign, use_llm=False)
    ids = [i["id"] for i in client.get(
        "/api/opportunities", params={"campaign_id": campaign["id"]}
    ).json()["items"]]

    # FR-284: a rejection must carry a reason, because FR-285 learns from it.
    bad = client.patch(f"/api/opportunities/{ids[0]}", json={"user_status": "not_interested"})
    assert bad.status_code == 400
    ok = client.patch(
        f"/api/opportunities/{ids[0]}",
        json={"user_status": "not_interested", "not_interested_reason": "salary too low"},
    )
    assert ok.status_code == 200
    assert ok.json()["user_status"] == "not_interested"

    reorder = client.post(
        "/api/opportunities/reorder",
        json={"campaign_id": campaign["id"], "ordered_ids": [ids[-1]]},
    )
    assert reorder.status_code == 200
    listed = client.get(
        "/api/opportunities", params={"campaign_id": campaign["id"]}
    ).json()["items"]
    assert listed[0]["id"] == ids[-1]

    compare = client.post("/api/opportunities/compare", json={"opportunity_ids": ids})
    assert compare.status_code == 200
    assert len(compare.json()["matrix"]["title"]) == len(ids)

    weights = client.put(
        "/api/opportunities/weights", json={"weights": {"dream_fit": 0.5}}
    )
    assert weights.status_code == 200
    assert sum(weights.json()["weights"].values()) == pytest.approx(1.0)
    assert client.put(
        "/api/opportunities/weights", json={"weights": {"nonsense": 1.0}}
    ).status_code == 400

    meter = client.get(f"/api/opportunities/{ids[0]}/dream-fit")
    assert meter.status_code == 200
    assert "met" in meter.json()


def test_api_refuses_generated_material_that_claims_a_vacancy(api):
    client, seeker_id, campaign, company_id = api
    opportunity_id = repo.create_opportunity(
        seeker_id,
        campaign["id"],
        {
            "company_id": company_id,
            "kind": "speculative",
            "title": "Head of Data",
            "plausibility": 0.6,
            "language": "en",
        },
    )
    response = client.post(
        f"/api/opportunities/{opportunity_id}/check-material",
        json={"text": "I would like to apply for the vacancy you advertised."},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["is_speculative"] is True
    assert body["safe"] is False
    assert body["false_vacancy_claims"]


# ---------------------------------------------------------------------------
# FR-149 / FR-262: the spontaneous-application track is reachable
# ---------------------------------------------------------------------------


def test_speculative_is_a_re_runnable_pipeline_stage():
    """NFR-603: the track has to be on the re-run surface, not only in the API.

    ``speculative.generate_campaign`` was reachable from exactly one place - a
    hand-written POST. No screen called it, no stage ran it, and nothing chained
    it after synthesis, so the whole unadvertised-roles half of the product was
    dead code from a user's point of view.
    """
    from dreamjob.pipeline import collection

    stages = {s["stage"]: s["description"] for s in collection.available_stages()}
    assert "speculative" in stages, "the spontaneous track is not a re-runnable stage"
    assert "FR-262" in stages["speculative"]


def test_the_stage_generates_openings_and_reports_what_it_did(db, monkeypatch):
    seeker_id = _seeker()
    campaign = _campaign(seeker_id)
    company_id = _company("Northwind Data")
    _campaign_company(campaign["id"], company_id)

    stub = StubLLM(
        {
            "openings": [
                {
                    "title": "Head of Data Platform",
                    "function_family": "data & analytics",
                    "seniority": "director",
                    "description": "Would own the platform the company has no owner for.",
                    "rationale": "No data leadership in the department map.",
                    "evidence": ["no data leadership in the map"],
                    "plausibility": 0.7,
                    "plausibility_basis": "structure",
                }
            ],
            "company_read": "Growing, no data lead.",
            "insufficient_evidence": False,
        }
    )
    monkeypatch.setattr(speculative, "LLMClient", lambda **kwargs: stub)

    report = speculative.rerun(campaign["id"], seeker_id)

    assert report["created"] == 1
    assert report["companies_generated"] == 1
    rows = repo.list_opportunities(seeker_id, campaign_id=campaign["id"])
    assert [r["kind"] for r in rows] == ["speculative"]


def test_the_stage_answers_a_missing_consent_rather_than_raising(db, monkeypatch):
    """CR-410: a stage re-run reports what happened; the caller renders it."""
    seeker_id = _seeker()
    campaign = _campaign(seeker_id)
    # Withdraw the consent the fixture recorded.
    from dreamjob.db.connection import execute

    execute("DELETE FROM consent_record WHERE job_seeker_id = ?", (seeker_id,))
    monkeypatch.setattr(speculative, "LLMClient", lambda **kwargs: StubLLM({}))

    result = speculative.rerun(campaign["id"], seeker_id)
    assert result["error"] == "consent_required"
    assert "CR-410" in result["detail"]


def test_a_spontaneous_only_campaign_is_recognised_as_one(db):
    """FR-149: the directive that drops every vacancy source is what marks it.

    Such a campaign plans no job board and no ATS, so synthesising its collected
    vacancies can only ever return an empty list. Something has to notice.
    """
    seeker_id = _seeker()
    campaign = _campaign(seeker_id)
    assert speculative.is_spontaneous_campaign(campaign) is False

    update_row(
        "directive_set", campaign["directive_set_id"], {"spontaneous_only": 1}
    )
    assert speculative.is_spontaneous_campaign(campaign) is True


def test_an_unreadable_directive_set_is_not_spontaneous_only(db, monkeypatch):
    """A configuration this cannot read must not silently turn the track on."""
    seeker_id = _seeker()
    campaign = _campaign(seeker_id)

    def boom(_campaign):
        raise RuntimeError("directive set unreadable")

    monkeypatch.setattr(speculative.campaign_repo, "load_planning_inputs", boom)
    assert speculative.is_spontaneous_campaign(campaign) is False


# ---------------------------------------------------------------------------
# FR-264: the Glassdoor path, and the pass that fills the columns in
# ---------------------------------------------------------------------------


def test_glassdoor_snapshots_price_a_role_without_reaching_the_shared_base(db, monkeypatch):
    """FR-264/FR-265: a campaign's Glassdoor run feeds the estimate, advisory only.

    The snapshots stay campaign-scoped (they are never promoted into
    ``compensation_observation``), and they are matched on company *and* role.
    """
    company_id = _company("Vestor NV")
    advisory = {
        "employers": [
            {"company_name": "Vestor NV", "rating": 3.6, "review_count": 88,
             "themes": {"pros": ["clear roadmap"], "cons": ["slow reviews"]}},
        ],
        "salaries": [
            {"company_name": "Vestor NV", "job_title": "Data Engineering Manager",
             "low": 88_000, "high": 112_000, "currency": "EUR", "sample_size": 24,
             "url": "https://www.glassdoor.com/Salary/vestor.htm"},
            # Same employer, unrelated role: must not be treated as comparable.
            {"company_name": "Vestor NV", "job_title": "Warehouse Operative",
             "low": 24_000, "high": 29_000, "currency": "EUR", "sample_size": 40},
            # Another employer entirely.
            {"company_name": "Someone Else BV", "job_title": "Data Engineering Manager",
             "low": 10_000, "high": 12_000, "currency": "EUR"},
        ],
    }
    opportunity = {
        "id": "o-gd",
        "company_id": company_id,
        "company_name": "Vestor NV",
        "title": "Data Engineering Manager",
        "function_family": "data & analytics",
        "seniority": "manager",
        "country": "BE",
        "comp_is_stated": 0,
    }

    result = comp_mod.estimate(opportunity, advisory=advisory)

    used = [s for s in result.sources if s.used]
    assert [s.kind for s in used] == ["glassdoor"]
    assert used[0].sample_size == 24
    assert (result.comp_min, result.comp_max) == (88_000, 112_000)
    # A source kind may not talk itself past its own ceiling, however big its sample.
    assert result.confidence <= comp_mod.SOURCE_CONFIDENCE_CEILING["glassdoor"]
    # FR-265: the rating rides along, advisory, and does not move the range.
    assert result.employer_rating == 3.6
    assert {t["polarity"] for t in result.employer_review_themes} == {"pros", "cons"}
    # Nothing was written into the shared knowledge base.
    assert query_one("SELECT COUNT(*) AS n FROM compensation_observation")["n"] == 0
    assert query_one("SELECT COUNT(*) AS n FROM employer_review_summary")["n"] == 0


def test_campaign_pass_prices_every_opportunity(db, monkeypatch):
    """The FR-264 pass fills the columns the detail screen reads (FR-283)."""
    seeker_id = _seeker()
    campaign = _campaign(seeker_id)
    campaign_id = campaign["id"]
    company_id = _company("Priced NV")
    for index in range(4):
        insert_row(
            "vacancy",
            {
                "company_id": _company(f"Comparable {index}"),
                "title": "Data Engineering Manager",
                "function_family": "data & analytics",
                "seniority": "manager",
                "country": "BE",
                "salary_min": 70_000 + index * 1_000,
                "salary_max": 90_000 + index * 1_000,
                "salary_currency": "EUR",
                "collected_at": utcnow(),
            },
        )
    insert_row(
        "opportunity",
        {
            "id": "o-unpriced",
            "job_seeker_id": seeker_id,
            "campaign_id": campaign_id,
            "company_id": company_id,
            "title": "Data Engineering Manager",
            "function_family": "data & analytics",
            "seniority": "manager",
            "country": "BE",
            "kind": "vacancy",
            "comp_is_stated": 0,
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )
    monkeypatch.setattr(comp_mod, "campaign_advisory", lambda _cid: {"employers": [], "salaries": []})

    report = comp_mod.enrich_campaign(seeker_id, campaign_id)

    assert report["estimated"] == 1
    assert report["unpriced"] == 0
    stored = repo.get_opportunity("o-unpriced", seeker_id)
    assert stored["comp_max"] is not None
    assert stored["comp_currency"] == "EUR"
    assert stored["comp_sources"]["sources"]


def test_scoring_reads_the_employer_rating_from_the_campaigns_own_snapshots(db):
    """FR-265: the rating lives in the browser run, not in the shared base.

    ``employer_review_summary`` is only ever filled by a browser run that
    promotes its findings, which nothing does; read without the campaign's
    snapshots the rating is ``None`` for every employer and the component is
    silently absent from the score.
    """
    company_id = _company("Rated NV")
    advisory = {
        "employers": [{"company_name": "Rated NV", "rating": 4.2, "review_count": 310,
                       "themes": {"pros": ["real ownership"]}}],
        "salaries": [],
    }

    without = scoring.company_attractiveness(company_id, company={"name": "Rated NV"})
    assert not any("review rating" in r for r in without.reasons)

    with_snapshots = scoring.company_attractiveness(
        company_id, company={"name": "Rated NV"}, advisory=advisory
    )
    assert any("employer review rating 4.2/5" in r for r in with_snapshots.reasons)
    # FR-265: advisory only - it is one visible component, never the score.
    assert any("advisory only" in r for r in with_snapshots.reasons)
