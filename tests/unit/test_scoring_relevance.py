"""Ranking relevance: missing evidence must never outrank evidence (FR-281, FR-383).

Each test pins one failure seen on a real seeker's list, where an
administrative vacancy and a machinist's advert out-ranked the senior
software and applied-AI roles the seeker was looking for.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from dreamjob.pipeline import scoring
from dreamjob.pipeline.opportunities import infer_function_family


def _ctx(**overrides) -> scoring.ScoringContext:
    ctx = scoring.ScoringContext(job_seeker_id="seeker", campaign_id="campaign", weights={})
    for key, value in overrides.items():
        setattr(ctx, key, value)
    return ctx


def test_one_listed_skill_is_weak_evidence():
    held = {"document management", "python", "react", "postgresql", "docker"}
    one_of_one = scoring._skill_overlap(["Document management"], held)
    most_of_many = scoring._skill_overlap(
        ["Python", "React", "PostgreSQL", "Docker", "Kubernetes", "Go"], held
    )
    assert one_of_one < 0.6
    assert most_of_many > one_of_one


def test_unknown_criteria_do_not_inflate_the_dream_fit():
    """One met criterion among thirty unknowns used to score 100%."""
    criteria = [{"id": "role_family:1", "status": "met", "importance": "strong"}] + [
        {"id": f"responsibility:{i}", "status": "unknown", "importance": "strong"}
        for i in range(30)
    ]
    value = scoring.score_criteria(criteria)
    assert value == pytest.approx(scoring.NEUTRAL, abs=0.05)


def test_cannot_assess_stays_out_of_the_dream_fit():
    criteria = [
        {"id": "target_role:1", "status": "met", "importance": "strong"},
        {"id": "company_characteristic:1", "status": "cannot_assess", "importance": "must"},
    ]
    assert scoring.score_criteria(criteria) == pytest.approx(1.0)


def test_nothing_assessed_is_not_a_score():
    assert scoring.score_criteria([{"id": "x", "status": "unknown"}]) is None
    assert scoring._weighted_neutral([(1.0, None), (2.0, None)]) is None


def test_missing_components_count_as_neutral_not_as_absent():
    # Renormalising would make this 1.0: the only known component is perfect.
    assert scoring._weighted_neutral([(0.1, 1.0), (0.9, None)]) == pytest.approx(0.55)


def test_title_relevance_separates_the_target_role_from_unrelated_work():
    ctx = _ctx(
        role_titles=scoring._role_titles(
            [{"title": "Senior Full-Stack Software Engineer"},
             {"title": "Security-minded application developer"}],
            [{"family": "AI & data engineering", "example_titles": ["Applied AI Engineer"]}],
            None,
        )
    )

    def rel(title: str) -> float:
        value, _ = scoring.title_relevance({"title": title}, ctx)
        return value

    assert rel("Full-Stack Engineer") == pytest.approx(1.0)
    assert rel("AI Engineer") == pytest.approx(1.0)
    assert rel("administratief medewerker") == 0.0
    assert rel("Tourneur-Fraiseur") == 0.0
    # Generic words alone ("engineer") count for little.
    assert rel("Mechanical Engineer") < 0.3
    # Dutch titles are compared in the language the dream model is written in.
    assert rel("Full Stack Ontwikkelaar") > 0.6


def test_posting_text_evidence_weighs_distinctive_skills_over_generic_ones():
    terms = scoring._skill_terms(
        ["Teamwork", "Sales",
         "Full-stack web application delivery (Python/FastAPI, React, PostgreSQL)",
         "Large language models"]
    )
    # The bracketed technologies become terms of their own.
    assert {"python", "fastapi", "react", "postgresql"} <= set(terms)
    weights = {label: 0.2 for label in terms}
    weights.update({"fastapi": 5.0, "python": 2.5, "react": 3.0, "postgresql": 3.5,
                    "large language models": 3.0})
    ctx = _ctx(skill_terms=terms, _term_weights=weights)

    tech, tech_hits = scoring.posting_skill_evidence(
        {"title": "Full-Stack Engineer",
         "description": "You build our platform in Python and FastAPI on PostgreSQL, "
                        "with a React front end and LLMs behind the agents."},
        ctx,
    )
    generic, _ = scoring.posting_skill_evidence(
        {"title": "Account Executive",
         "description": "Strong teamwork and sales skills for our enterprise customers."},
        ctx,
    )
    assert {"fastapi", "postgresql", "react", "large language models"} <= set(tech_hits)
    assert tech > 0.8
    assert generic < 0.2


def test_seniority_is_measured_against_the_target_not_the_last_title():
    directives = SimpleNamespace(
        job_content=SimpleNamespace(
            seniority_min="lead", seniority_max="principal", seniority_range=lambda: (4, 5)
        )
    )
    ctx = _ctx(directives=directives, composite={"seniority": {"level": "director"}})
    assert scoring._seeker_seniority(ctx) == (4, 5)
    ctx_without = _ctx(composite={"seniority": {"level": "director"}})
    assert scoring._seeker_seniority(ctx_without) == (6, 6)


def test_a_previous_semantic_judgement_survives_a_deterministic_rescore():
    stored = {
        "components": {
            "dream_fit": {
                "method": "llm+deterministic",
                "reasons": ["close to the stated ideal"],
                "detail": {"semantic": 0.82, "deterministic": 0.4},
            }
        },
        "strongest_match": "FastAPI",
    }
    carried = scoring._previous_semantic(
        {"score_detail": json.dumps(stored), "rationale": "A good match."}
    )
    assert carried["dream_fit"] == 0.82
    assert carried["rationale"] == "A good match."
    assert carried["carried_forward"] is True

    deterministic = {"components": {"dream_fit": {"method": "deterministic", "detail": {}}}}
    assert scoring._previous_semantic({"score_detail": deterministic}) is None


def test_one_word_in_an_advert_body_does_not_set_the_function_family():
    machinist = (
        "Usiner des pièces métalliques sur tours et fraiseuses. "
        "Programmer, régler et optimiser les machines-outils."
    )
    assert infer_function_family("Tourneur-Fraiseur", machinist) is None
    assert infer_function_family("Software Engineer", "") == "software engineering"
    body = "As a developer you join our software team and review other developers' code."
    assert infer_function_family("Team member", body) == "software engineering"


def test_a_negated_deal_breaker_does_not_fire_on_the_wanted_work(monkeypatch):
    """'No hands-on design or development' must not fire on an advert offering both."""
    tag = SimpleNamespace(employer_disclosed=True, is_intermediary=False)
    ctx = _ctx(dream={"deal_breakers": [{
        "constraint": "Pure management roles with no hands-on design or development",
        "hard": True, "detectable_from": ["vacancy text", "job title"],
    }]})
    monkeypatch.setattr(ctx, "company", lambda _id: {})
    wanted = {"title": "Senior Software Engineer",
              "description": "Hands-on design and development of our web platform."}
    criteria = scoring.dream_criteria(wanted, ctx, tag=tag)
    assert criteria[0]["status"] == "unknown"
    # A short constraint the title states outright still fires.
    ctx.dream = {"deal_breakers": [{"constraint": "door-to-door sales", "hard": True}]}
    criteria = scoring.dream_criteria(
        {"title": "Door-to-door Sales Representative", "description": ""}, ctx, tag=tag
    )
    assert criteria[0]["status"] == "violated"


def test_an_old_advert_is_less_plausible_than_a_new_one():
    from datetime import UTC, datetime, timedelta

    def age(days: int) -> float:
        posted = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        return scoring.plausibility_fit({"kind": "vacancy", "posted_at": posted}).value

    assert age(10) == 1.0
    assert age(200) < age(60) < 1.0
    assert age(900) == pytest.approx(scoring.STALE_FLOOR)
    assert scoring.plausibility_fit({"kind": "vacancy"}).value == pytest.approx(0.8)


def test_employer_attractiveness_counts_only_as_far_as_the_job_fits():
    weights = scoring.normalise_weights(None)

    def subs(fit: float) -> dict:
        return {
            "profile_fit": scoring.SubScore(fit),
            "dream_fit": scoring.SubScore(fit),
        }

    assert scoring.fit_gate(subs(0.8), weights) == 1.0
    assert scoring.fit_gate(subs(0.3), weights) == pytest.approx(0.5)


def test_place_queries_name_one_place_or_none():
    from dreamjob.pipeline.locate import place_queries

    assert place_queries("BE") == ([], [])
    assert place_queries("Remote job") == ([], [])
    assert place_queries("Ghent, Oost-Vlaanderen, Belgium")[0] == [
        "Ghent, Oost-Vlaanderen, Belgium", "Ghent",
    ]
    assert place_queries("Antwerp / Ghent, Vlaams Gewest, Belgium")[0] == ["Antwerp"]
    assert place_queries("Bonn oder freie Arbeitsplatzwahl")[0] == ["Bonn"]
    assert place_queries("Leuven Materialise") == (
        ["Leuven Materialise"], ["Materialise", "Leuven"],
    )


def test_a_count_of_sites_is_not_a_place():
    from dreamjob.pipeline.locate import place_queries

    assert place_queries("3 Locations") == ([], [])
    assert place_queries("Multiple locations") == ([], [])


def test_a_prominent_place_beats_the_rows_country_hint(monkeypatch):
    import asyncio

    from dreamjob.pipeline import geocode as geo
    from dreamjob.pipeline import locate

    def result(cc: str, importance: float) -> geo.GeocodeResult:
        return geo.GeocodeResult(query="q", display_name="x", latitude=1.0, longitude=2.0,
                                 country_code=cc, importance=importance)

    async def fake(query, *, country_codes=None, egress=None, **_):
        if country_codes:
            return [result("BE", 0.2)]      # a Belgian street called Barcelona
        return [result("ES", 0.8) if query == "Barcelona" else result("NL", 0.3)]

    monkeypatch.setattr(geo, "search", fake)
    city = asyncio.run(locate._resolve("Barcelona", ["BE"], None))
    assert city.country_code == "ES"
    # An obscure place abroad loses to the country-restricted match.
    village = asyncio.run(locate._resolve("Zellik", ["BE"], None))
    assert village.country_code == "BE"


def test_a_place_missing_from_the_hinted_country_is_found_abroad(monkeypatch):
    import asyncio

    from dreamjob.pipeline import geocode as geo
    from dreamjob.pipeline import locate

    async def fake(query, *, country_codes=None, egress=None, **_):
        if country_codes:
            return []
        return [geo.GeocodeResult(query=query, display_name="Almere", latitude=52.37,
                                  longitude=5.21, country_code="NL", importance=0.45)]

    monkeypatch.setattr(geo, "search", fake)
    assert asyncio.run(locate._resolve("Almere", ["BE"], None)).country_code == "NL"
