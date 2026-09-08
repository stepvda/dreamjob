"""Dream-job intelligence, networking radar and campaign export.

Requirements exercised: FR-381, FR-382, FR-384, FR-385, FR-443, FR-462,
FR-463, NFR-104, NFR-301, CR-405.

Everything runs against a throw-away SQLite file with no network.  The three
steps that would normally call DeepSeek - sharpening the gap actions, writing
the stepping-stone rationales and drafting the LinkedIn text - are driven by a
stub client implementing the one method the modules use, so the JSON contract
of each prompt is exercised without egress.
"""

from __future__ import annotations

import json
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from dreamjob.adapters.events import radar
from dreamjob.config import get_settings
from dreamjob.db.connection import insert_row, query_one, update_row, utcnow
from dreamjob.db.migrator import migrate
from dreamjob.db.repositories import intelligence as repo
from dreamjob.exporting import campaign_export, json_export
from dreamjob.intelligence import events as events_mod
from dreamjob.intelligence import gap_analysis as gaps_mod
from dreamjob.intelligence import linkedin_advice as linkedin_mod
from dreamjob.intelligence import stepping_stones as stones_mod
from dreamjob.intelligence import values_match as values_mod


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DREAMJOB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DREAMJOB_DB_PATH", str(tmp_path / "data" / "test.db"))
    get_settings.cache_clear()
    get_settings()
    migrate()
    yield
    get_settings.cache_clear()


class StubLLM:
    """The one member of ``LLMClient`` these modules touch."""

    def __init__(self, response) -> None:
        self.response = response
        self.calls: list[dict] = []

    def complete_json(self, task, system, user, **kwargs):
        self.calls.append({"task": task, "system": system, "user": user, "kwargs": kwargs})
        return self.response(self.calls[-1]) if callable(self.response) else self.response


def _iso(days: float) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# A small, complete campaign
# ---------------------------------------------------------------------------


def _seeker(email: str = "seeker@example.com", name: str = "Test Seeker") -> str:
    return insert_row(
        "job_seeker",
        {"email": email, "display_name": name, "created_at": utcnow(), "updated_at": utcnow()},
    )


def _profile(seeker_id: str) -> str:
    return insert_row(
        "profile_version",
        {
            "job_seeker_id": seeker_id,
            "version": 1,
            "sections": {
                "summary": "Data engineer with eight years on batch and streaming pipelines.",
                "top_skills": ["Python", "SQL"],
                "languages": [{"name": "English", "level": "Native"}],
                "experience": [
                    {"title": "Senior Data Engineer", "company": "Northwind BV", "start": "2019"}
                ],
                "certifications": [],
            },
            "source_note": "manual",
            "created_at": utcnow(),
        },
    )


def _composite(seeker_id: str, profile_version_id: str) -> str:
    return insert_row(
        "composite_profile",
        {
            "job_seeker_id": seeker_id,
            "profile_version_id": profile_version_id,
            "version": 1,
            "narrative": "Eight years building data platforms for logistics companies.",
            "core_competencies": [
                {"id": "core_competencies:1", "text": "Python", "depth": "expert"},
                {"id": "core_competencies:2", "text": "SQL", "depth": "expert"},
            ],
            "adjacent_competencies": [],
            "seniority": {"id": "seniority:1", "level": "senior_ic", "text": "Senior engineer"},
            "domains": [{"id": "domains:1", "text": "logistics"}],
            "achievements": [
                {"id": "achievements:1", "text": "Cut nightly batch runtime from 6h to 40min"}
            ],
            "public_footprint": [],
            "created_at": utcnow(),
        },
    )


def _dream(seeker_id: str) -> str:
    return insert_row(
        "dream_job_model",
        {
            "job_seeker_id": seeker_id,
            "version": 1,
            "statement": "I want to lead a data platform team at a company that gives its "
            "engineers autonomy.",
            "target_roles": [
                {"title": "Head of Data Platform", "seniority": "manager", "priority": 1}
            ],
            "role_families": [{"family": "data engineering leadership"}],
            "responsibilities": [
                {"activity": "run a platform team", "importance": "must"},
                {"activity": "own the kubernetes migration", "importance": "strong"},
            ],
            "company_characteristics": [
                {"attribute": "size", "value": "scaleup", "importance": "strong"}
            ],
            "culture_values": [
                {"cue": "autonomy", "polarity": "seek", "why": "wants ownership"},
                {"cue": "micromanagement", "polarity": "avoid", "why": "left a job over it"},
            ],
            "deal_breakers": [],
            "created_at": utcnow(),
        },
    )


def _directives(seeker_id: str, *, discretion: int = 0, excluded: list | None = None) -> str:
    return insert_row(
        "directive_set",
        {
            "job_seeker_id": seeker_id,
            "name": "default",
            "version": 1,
            "job_content": {},
            "company_type": {},
            "location": {
                "countries": ["BE"],
                "home_location": {
                    "label": "Ghent",
                    "latitude": 51.05,
                    "longitude": 3.72,
                    "radius_km": 25,
                },
                "max_commute_minutes": 60,
            },
            "work_arrangement": {"travel_tolerance": "occasional"},
            "compensation": {},
            "discretion_mode": discretion,
            "discretion_excluded_companies": excluded or [],
            "created_at": utcnow(),
        },
    )


def _campaign(seeker_id: str, directive_id: str, profile_id: str, composite: str, dream: str):
    return insert_row(
        "campaign",
        {
            "job_seeker_id": seeker_id,
            "directive_set_id": directive_id,
            "profile_version_id": profile_id,
            "composite_profile_id": composite,
            "dream_job_model_id": dream,
            "name": "Autumn campaign",
            "status": "completed",
            "created_at": utcnow(),
        },
    )


def _company(name: str, *, values_culture=None, domain: str | None = None) -> str:
    return insert_row(
        "company",
        {
            "name": name,
            "normalised_name": name.lower(),
            "domain": domain or f"{name.lower().replace(' ', '')}.example",
            "country": "BE",
            "size_band": "51-200",
            "stage": "scaleup",
            "business_summary": f"{name} builds logistics software.",
            "values_culture": values_culture,
            "collected_at": utcnow(),
        },
    )


def _vacancy(company_id: str, title: str, *, language: str = "en") -> str:
    return insert_row(
        "vacancy",
        {
            "company_id": company_id,
            "title": title,
            "description": "A role on the data platform team.",
            "language": language,
            "collected_at": utcnow(),
        },
    )


def _opportunity(
    seeker_id: str,
    campaign_id: str,
    company_id: str,
    *,
    title: str,
    required: list[str],
    description: str,
    seniority: str = "senior",
    language: str = "en",
    score: float = 60.0,
    dream_fit: float = 40.0,
    profile_fit: float = 62.0,
    seniority_score: float | None = None,
    vacancy_id: str | None = None,
    function_family: str = "data engineering",
) -> str:
    detail = {"required_skill_overlap": 0.4}
    if seniority_score is not None:
        detail["seniority_score"] = seniority_score
    return insert_row(
        "opportunity",
        {
            "job_seeker_id": seeker_id,
            "campaign_id": campaign_id,
            "company_id": company_id,
            "vacancy_id": vacancy_id,
            "kind": "vacancy",
            "title": title,
            "function_family": function_family,
            "seniority": seniority,
            "description": description,
            "required_skills": required,
            "language": language,
            "country": "BE",
            "score": score,
            "score_dream_fit": dream_fit,
            "score_profile_fit": profile_fit,
            "score_detail": {"components": {"profile_fit": {"detail": detail}}},
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )


@pytest.fixture()
def campaign(db):
    """One seeker, one campaign, four scored opportunities."""
    seeker_id = _seeker()
    profile_id = _profile(seeker_id)
    composite_id = _composite(seeker_id, profile_id)
    dream_id = _dream(seeker_id)
    directive_id = _directives(seeker_id)
    campaign_id = _campaign(seeker_id, directive_id, profile_id, composite_id, dream_id)

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

    acme = _company("Acme Logistics")
    beta = _company("Beta Freight")
    vacancy_id = _vacancy(acme, "Data Platform Lead")
    dutch_vacancy = _vacancy(beta, "Data Engineer", language="nl")

    opportunities = [
        _opportunity(
            seeker_id, campaign_id, acme,
            title="Data Platform Lead",
            required=["Python", "Kubernetes", "Terraform"],
            description=(
                "You will lead a team of five engineers, own the platform roadmap and speak "
                "at the yearly conference. PMP certification is a plus."
            ),
            seniority="director",
            seniority_score=0.5,
            vacancy_id=vacancy_id,
            profile_fit=64.0,
            dream_fit=45.0,
        ),
        _opportunity(
            seeker_id, campaign_id, beta,
            title="Senior Data Engineer",
            required=["Python", "Kubernetes", "dbt"],
            description=(
                "Nederlandstalig team. Je zult een team leiden en spreken op een conference. "
                "PMP is een pluspunt."
            ),
            language="nl",
            vacancy_id=dutch_vacancy,
            profile_fit=58.0,
            dream_fit=35.0,
        ),
        _opportunity(
            seeker_id, campaign_id, acme,
            title="Platform Engineer",
            required=["Terraform", "SQL"],
            description="Manage a team of two, publish a blog about the platform.",
            language="nl",
            profile_fit=52.0,
            dream_fit=30.0,
        ),
        _opportunity(
            seeker_id, campaign_id, beta,
            title="Analytics Engineer",
            required=["SQL", "dbt"],
            description="Own the reporting stack. Fluent Dutch required.",
            profile_fit=40.0,
            dream_fit=25.0,
        ),
    ]
    return {
        "seeker_id": seeker_id,
        "campaign_id": campaign_id,
        "profile_id": profile_id,
        "composite_id": composite_id,
        "dream_id": dream_id,
        "directive_id": directive_id,
        "companies": {"acme": acme, "beta": beta},
        "opportunities": opportunities,
    }


# ---------------------------------------------------------------------------
# FR-381: gap analysis
# ---------------------------------------------------------------------------


def test_gap_analysis_produces_actionable_gaps(campaign):
    """FR-381: at least three gaps, each with an action, an effort and a link."""
    report = gaps_mod.analyse(campaign["seeker_id"], campaign["campaign_id"], use_llm=False)

    assert len(report.gaps) >= 3
    for gap in report.gaps:
        assert gap.closing_action.get("action"), gap.id
        assert gap.closing_action.get("kind") in {
            "course", "certification", "project", "publication", "role_type", "talk"
        }
        assert gap.effort.get("months") and gap.effort.get("level")
        assert gap.dimension in gaps_mod.DIMENSIONS

    dimensions = {gap.dimension for gap in report.gaps}
    assert {"skill", "certification", "language"} <= dimensions

    # The decisive link is computed, not asserted: Kubernetes is required by two
    # postings and evidenced by none, and each link carries the profile-fit
    # points it costs that opportunity.
    kubernetes = next(g for g in report.gaps if g.id == "skill:kubernetes")
    assert kubernetes.demand["postings"] == 2
    assert len(kubernetes.decisive_opportunities) == 2
    assert all(link.points_lost > 0 for link in kubernetes.decisive_opportunities)
    titles = {link.title for link in kubernetes.decisive_opportunities}
    assert titles == {"Data Platform Lead", "Senior Data Engineer"}

    # One skill of three missing costs a third of the required-skill overlap,
    # which is the whole of the profile-fit sub-score here.
    assert kubernetes.decisive_opportunities[0].points_lost == pytest.approx(33.3, abs=0.5)


def test_gap_analysis_links_only_where_the_gap_bites(campaign):
    """A gap is linked to the postings that lost points on it, and no others."""
    report = gaps_mod.analyse(campaign["seeker_id"], campaign["campaign_id"], use_llm=False)
    terraform = next(g for g in report.gaps if g.id == "skill:terraform")
    linked = {link.title for link in terraform.decisive_opportunities}
    assert linked == {"Data Platform Lead", "Platform Engineer"}
    assert "Analytics Engineer" not in linked


def test_gap_analysis_is_persisted_per_campaign(campaign):
    gaps_mod.analyse(campaign["seeker_id"], campaign["campaign_id"], use_llm=False)
    stored = repo.get_gap_analysis(campaign["seeker_id"], campaign["campaign_id"])
    assert stored is not None
    assert stored["generated_by"] == "deterministic"
    assert len(stored["gaps"]) >= 3

    # Re-running replaces rather than stacks.
    gaps_mod.analyse(campaign["seeker_id"], campaign["campaign_id"], use_llm=False)
    assert len(repo.list_gap_analyses(campaign["seeker_id"])) == 1


def test_gap_analysis_llm_refinement(campaign):
    """The model sharpens actions; the computed gaps and links are untouched."""
    deterministic = gaps_mod.analyse(
        campaign["seeker_id"], campaign["campaign_id"], use_llm=False, persist=False
    )
    target = deterministic.gaps[0]

    llm = StubLLM(
        {
            "gaps": [
                {
                    "id": target.id,
                    "action": "Take the CKA course and run the cluster for the nightly jobs",
                    "detail": "Book the exam for March.",
                    "effort_months": 2,
                    "effort_detail": "evenings for eight weeks",
                }
            ],
            "summary": "Two gaps stand between this profile and the dream job.",
        }
    )
    report = gaps_mod.analyse(
        campaign["seeker_id"], campaign["campaign_id"], llm=llm, persist=False
    )

    assert llm.calls and llm.calls[0]["task"] == "analysis.gap"
    # NFR-205: the computed analysis travels as untrusted data, not as instructions.
    assert "analysis" in llm.calls[0]["kwargs"]["untrusted"]
    assert report.generated_by == "llm+deterministic"
    refined = next(g for g in report.gaps if g.id == target.id)
    assert refined.closing_action["action"].startswith("Take the CKA course")
    assert refined.effort["months"] == 2
    assert refined.dimension == target.dimension
    assert len(refined.decisive_opportunities) == len(target.decisive_opportunities)


def test_gap_analysis_survives_an_unreachable_model(campaign):
    """NFR-104: a failing model costs phrasing, not the analysis."""

    class Failing:
        def complete_json(self, *args, **kwargs):
            from dreamjob.llm.client import LLMError

            raise LLMError("no provider")

    report = gaps_mod.analyse(
        campaign["seeker_id"], campaign["campaign_id"], llm=Failing(), persist=False
    )
    assert report.generated_by == "deterministic"
    assert len(report.gaps) >= 3


# ---------------------------------------------------------------------------
# FR-382: stepping stones
# ---------------------------------------------------------------------------


def test_stepping_stones_trigger_below_the_threshold(campaign):
    """FR-382: nothing clears 65, so two to three paths are proposed."""
    state = stones_mod.assess(campaign["seeker_id"], campaign["campaign_id"])
    assert state["threshold"] == stones_mod.DEFAULT_THRESHOLD
    assert state["triggered"] is True
    assert state["destinations"] == []

    report = stones_mod.propose(campaign["seeker_id"], campaign["campaign_id"], use_llm=False)
    assert report.triggered
    assert 2 <= len(report.paths) <= 3
    for path in report.paths:
        assert [step.position for step in path.steps] == [1, 2, 3]
        assert path.steps[-1].kind == "destination"
        assert "Head of Data Platform" in path.steps[-1].role
        assert path.rationale
    # The first step is reachable now, so it names real opportunities.
    assert any(path.steps[0].opportunity_ids for path in report.paths)
    assert repo.list_stepping_stones(campaign["seeker_id"], campaign["campaign_id"])


def test_stepping_stones_do_not_fire_when_a_destination_exists(campaign):
    stones_mod.set_threshold(30.0, campaign["seeker_id"])
    state = stones_mod.assess(campaign["seeker_id"], campaign["campaign_id"])
    assert state["destinations"]
    report = stones_mod.propose(campaign["seeker_id"], campaign["campaign_id"], use_llm=False)
    assert report.triggered is False
    assert report.paths == []
    assert report.warnings


def test_stepping_stone_tags_merge_with_the_seekers_own(campaign):
    """FR-382 tags the list; FR-284 says the seeker's own tags survive it."""
    stones_mod.set_threshold(42.0, campaign["seeker_id"])
    repo.set_opportunity_tags(campaign["opportunities"][0], campaign["seeker_id"], ["favourite"])
    stones_mod.propose(campaign["seeker_id"], campaign["campaign_id"], use_llm=False, force=True)
    result = stones_mod.apply_tags(campaign["seeker_id"], campaign["campaign_id"])

    tags = repo.opportunity_tags(campaign["opportunities"][0], campaign["seeker_id"])
    assert "favourite" in tags
    assert stones_mod.DESTINATION_TAG in tags
    assert result["tagged"][stones_mod.DESTINATION_TAG] >= 1

    # An opportunity is either the destination or a step towards it, never both.
    for opportunity_id in campaign["opportunities"]:
        row_tags = repo.opportunity_tags(opportunity_id, campaign["seeker_id"]) or []
        assert not (
            stones_mod.DESTINATION_TAG in row_tags
            and stones_mod.STEPPING_STONE_TAG in row_tags
        )


def test_stepping_stones_llm_refinement(campaign):
    llm = StubLLM(
        {
            "paths": [
                {
                    "name": "Via data engineering",
                    "display_name": "Platform lead route",
                    "rationale": "Two moves, four years, and every step is winnable now.",
                    "steps": [
                        {"position": 2, "role": "Data platform lead", "rationale": "Scope."}
                    ],
                }
            ]
        }
    )
    report = stones_mod.propose(
        campaign["seeker_id"], campaign["campaign_id"], llm=llm, persist=False
    )
    assert llm.calls[0]["task"] == "plan.stepping_stones"
    assert report.generated_by == "llm+deterministic"
    assert any(p.name == "Platform lead route" for p in report.paths)


# ---------------------------------------------------------------------------
# FR-384: values match
# ---------------------------------------------------------------------------


def test_values_match_warns_on_contradiction_and_on_avoided_cues(campaign):
    company_id = _company(
        "Rigid Systems",
        values_culture={
            "stated_values": [
                {"value": "operational excellence", "evidence": "About page", "source": "web"}
            ],
            "working_style": [
                {"cue": "top-down decision making", "evidence": "Careers page"},
                {"cue": "micromanagement is our style", "evidence": "Glassdoor summary"},
            ],
            "employer_review_themes": [],
            "derived_from": ["website", "job_ads"],
            "confidence": 0.6,
        },
    )
    match = values_mod.for_company(
        campaign["seeker_id"], company_id, campaign_id=campaign["campaign_id"], use_llm=False
    )
    cues = {w.cue for w in match.warnings}
    assert "autonomy" in cues            # sought, contradicted by "top-down"
    assert "micromanagement" in cues     # avoided, and stated by the company
    assert match.score is not None and match.score < 0.5
    assert all(w.company_cues for w in match.warnings)

    document = values_mod.warnings_for_document(
        campaign["seeker_id"], company_id, campaign_id=campaign["campaign_id"]
    )
    assert document["address_in_letter"]
    assert document["company_name"] == "Rigid Systems"


def test_values_match_is_silent_when_the_material_is_silent(campaign):
    """CR-405: silence is an open question, not a warning."""
    company_id = _company(
        "Quiet Corp",
        values_culture={"stated_values": [{"value": "quality"}], "derived_from": ["website"]},
    )
    match = values_mod.for_company(campaign["seeker_id"], company_id, use_llm=False)
    assert match.warnings == []
    assert {u.cue for u in match.unknown} >= {"autonomy", "micromanagement"}


def test_values_match_llm_refinement(campaign):
    company_id = _company(
        "Rigid Systems 2",
        values_culture={"working_style": [{"cue": "top-down", "evidence": "Careers page"}]},
    )
    llm = StubLLM(
        {"warnings": [{"cue": "autonomy", "explanation": "Their careers page says top-down."}]}
    )
    match = values_mod.for_company(campaign["seeker_id"], company_id, llm=llm, use_llm=False)
    assert llm.calls[0]["task"] == "analysis.values"
    assert match.generated_by == "llm+deterministic"
    assert match.warnings[0].explanation == "Their careers page says top-down."


# ---------------------------------------------------------------------------
# FR-443: LinkedIn advice
# ---------------------------------------------------------------------------


def test_linkedin_advice_is_grounded_in_the_corpus(campaign):
    advice = linkedin_mod.suggest(
        campaign["seeker_id"], campaign["campaign_id"], use_llm=False
    )
    assert advice.corpus is not None and advice.corpus.postings == 4

    listed = {s["skill"] for s in advice.skills}
    assert "python" in listed or "sql" in listed
    for skill in advice.skills:
        assert skill["postings"] >= linkedin_mod.MIN_TERM_POSTINGS
        assert "of 4 matching postings" in skill["reason"]

    # A skill the corpus wants but the profile cannot evidence is not listed.
    assert "kubernetes" not in listed

    assert advice.headlines and all(h["text"] for h in advice.headlines)
    assert advice.about
    assert any("does not sign in to LinkedIn" in note for note in advice.notes)
    stored = repo.latest_advice(campaign["seeker_id"], campaign["campaign_id"])
    assert stored is not None and stored["headlines"]


def test_linkedin_advice_under_discretion_mode(db):
    """FR-385: no 'open to work', and the draft is marked as not-to-apply-now."""
    seeker_id = _seeker("quiet@example.com")
    profile_id = _profile(seeker_id)
    composite_id = _composite(seeker_id, profile_id)
    dream_id = _dream(seeker_id)
    directive_id = _directives(seeker_id, discretion=1)
    campaign_id = _campaign(seeker_id, directive_id, profile_id, composite_id, dream_id)

    advice = linkedin_mod.suggest(seeker_id, campaign_id, use_llm=False)
    payload = advice.as_dict()
    assert payload["discretion_mode"] is True
    assert payload["apply_now"] is False
    assert "open to work" in advice.notes[0].lower()
    assert not any("open to work" in json.dumps(h).lower() for h in advice.headlines)


def test_linkedin_advice_llm_refinement(campaign):
    llm = StubLLM(
        {
            "headlines": [
                {"text": "Data platform engineer | Python, SQL", "basis": "current role",
                 "claims": "only the title you hold"}
            ],
            "about": "I build data platforms for logistics companies.",
            "notes": ["Left out Kubernetes: not evidenced in the profile."],
        }
    )
    advice = linkedin_mod.suggest(
        campaign["seeker_id"], campaign["campaign_id"], llm=llm, persist=False
    )
    assert llm.calls[0]["task"] == "generate.linkedin"
    assert advice.generated_by == "llm+deterministic"
    assert advice.headlines[0]["text"].startswith("Data platform engineer")
    assert advice.about.startswith("I build data platforms")


def test_linkedin_edit_is_stored_and_nothing_is_sent(campaign):
    advice = linkedin_mod.suggest(campaign["seeker_id"], campaign["campaign_id"], use_llm=False)
    updated = linkedin_mod.save_edit(campaign["seeker_id"], advice.advice_id, "my own words")
    assert updated["edited_text"] == "my own words"
    assert updated["edited_at"]


# ---------------------------------------------------------------------------
# FR-462: the event radar
# ---------------------------------------------------------------------------

JSONLD_PAGE = """
<html><head><title>Data Days</title>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Event","name":"Data Days Ghent",
 "startDate":"__START__","endDate":"__END__",
 "eventAttendanceMode":"https://schema.org/OfflineEventAttendanceMode",
 "location":{"@type":"Place","name":"Ghent Expo",
   "address":{"@type":"PostalAddress","addressLocality":"Ghent","addressCountry":"BE"},
   "geo":{"@type":"GeoCoordinates","latitude":51.05,"longitude":3.72}},
 "organizer":{"@type":"Organization","name":"Data Community BE"},
 "performer":[{"@type":"Person","name":"Ann Peeters","jobTitle":"Head of Data",
   "affiliation":{"@type":"Organization","name":"Acme Logistics"}}],
 "url":"https://datadays.example/2026"}
</script></head><body></body></html>
"""


def test_event_adapter_reads_jsonld_and_ics():
    page = JSONLD_PAGE.replace("__START__", _iso(30)).replace("__END__", _iso(31))
    events = radar.parse_page(page, "https://datadays.example/2026")
    assert len(events) == 1
    event = events[0]
    assert event.name == "Data Days Ghent"
    assert event.country == "BE"
    assert event.kind == radar.KIND_CONFERENCE
    assert event.event_format == radar.FORMAT_IN_PERSON
    assert event.speakers[0]["company_name"] == "Acme Logistics"

    ics = (
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:Python Meetup Ghent\r\n"
        "DTSTART:20261201T183000Z\r\nDTEND:20261201T210000Z\r\n"
        "LOCATION:Ghent\\, Belgium\r\nURL:https://example.org/e/1\r\n"
        "END:VEVENT\r\nEND:VCALENDAR"
    )
    parsed = radar.parse_page(ics, "https://example.org/cal.ics")
    assert parsed[0].name == "Python Meetup Ghent"
    assert parsed[0].kind == radar.KIND_MEETUP
    assert parsed[0].starts_at.startswith("2026-12-01T18:30")


def test_social_event_adapter_declares_its_terms():
    """IR-101: Meetup and Eventbrite are restricted and need an acknowledgement."""
    assert radar.SocialEventAdapter.tos_status.value == "restricted"
    assert radar.SocialEventAdapter.requires_ack is True
    assert radar.EventRadarAdapter.tos_status.value == "permitted"


@pytest.mark.asyncio
async def test_restricted_event_adapter_refuses_without_acknowledgement(db):
    """IR-101: no catalogue acknowledgement, no Meetup/Eventbrite collection."""
    seeker_id = _seeker("radar@example.com")
    result = await events_mod.collect(
        seeker_id, [], keywords=["data engineering"], location="Ghent", use_social=True
    )
    assert result["skipped"] is True
    assert "acknowledgement" in result["reason"]
    assert result["collected"] == 0


def test_values_match_hides_a_company_excluded_by_discretion(campaign):
    """FR-385: the profile screen says why, instead of quietly showing nothing."""
    update_row(
        "directive_set",
        campaign["directive_id"],
        {
            "discretion_mode": 1,
            "discretion_excluded_companies": [
                {"name": "Acme Logistics", "reason": "current_employer"}
            ],
        },
    )
    match = values_mod.for_company(
        campaign["seeker_id"],
        campaign["companies"]["acme"],
        campaign_id=campaign["campaign_id"],
        use_llm=False,
    )
    assert match.discretion_mode is True
    assert match.excluded_reason == "current_employer"


def _event(name: str, *, days: float, latitude=51.05, longitude=3.72, speakers=None, **extra):
    return insert_row(
        "event",
        {
            "name": name,
            "starts_at": _iso(days),
            "ends_at": _iso(days + 0.2),
            "location": "Ghent",
            "country": "BE",
            "latitude": latitude,
            "longitude": longitude,
            "speakers": speakers or [],
            "attendees": speakers or [],
            "kind": "conference",
            "format": "in_person",
            "description": "A data platform conference.",
            "topics": ["data engineering"],
            "collected_at": utcnow(),
            "source": "events.radar",
            **extra,
        },
    )


def test_radar_matches_target_company_speakers_and_travel(campaign):
    near = _event(
        "Data Days Ghent",
        days=30,
        speakers=[{"name": "Ann Peeters", "role": "Head of Data",
                   "company_name": "Acme Logistics", "kind": "speaker"}],
    )
    far = _event("Data Summit Lisbon", days=40, latitude=38.7, longitude=-9.1)
    online = _event("Platform Webinar", days=10, latitude=None, longitude=None, format="online")

    result = events_mod.radar(campaign["seeker_id"], campaign["campaign_id"])
    ids = [e["id"] for e in result["events"]]

    assert near in ids                       # 0 km away, and Acme speaks
    assert online in ids                     # online events need no travel
    assert far not in ids                    # 1700 km, beyond "occasional" travel
    top = result["events"][0]
    assert top["id"] == near
    assert top["match"]["target_attendees"][0]["company_name"] == "Acme Logistics"
    assert top["match"]["distance_km"] is not None and top["match"]["distance_km"] < 5

    with_far = events_mod.radar(
        campaign["seeker_id"], campaign["campaign_id"], include_unreachable=True
    )
    assert far in [e["id"] for e in with_far["events"]]


def test_event_calendar_entry_and_company_link(campaign):
    event_id = _event(
        "Data Days Ghent",
        days=30,
        speakers=[{"name": "Ann Peeters", "company_name": "Acme Logistics", "kind": "speaker"}],
    )
    linked = events_mod.link_to_companies(
        campaign["seeker_id"], event_id, campaign["campaign_id"]
    )
    assert campaign["companies"]["acme"] in linked["linked_company_ids"]
    assert events_mod.for_company(campaign["companies"]["acme"])

    added = events_mod.add_to_calendar(
        campaign["seeker_id"], event_id, campaign_id=campaign["campaign_id"]
    )
    ics_path = Path(added["ics_path"])
    assert ics_path.is_file()
    body = ics_path.read_text(encoding="utf-8")
    assert "BEGIN:VEVENT" in body and "SUMMARY:Data Days Ghent" in body
    interest = repo.get_interest(campaign["seeker_id"], event_id)
    assert interest["status"] == events_mod.STATUS_GOING


def test_radar_drops_events_hosted_by_an_excluded_company(db):
    """FR-385: discretion mode keeps the search invisible, events included."""
    seeker_id = _seeker("discreet@example.com")
    profile_id = _profile(seeker_id)
    composite_id = _composite(seeker_id, profile_id)
    dream_id = _dream(seeker_id)
    directive_id = _directives(
        seeker_id,
        discretion=1,
        excluded=[{"name": "Northwind BV", "reason": "current_employer"}],
    )
    campaign_id = _campaign(seeker_id, directive_id, profile_id, composite_id, dream_id)

    hosted = _event("Northwind Tech Night", days=20)
    update_row("event", hosted, {"organiser": "Northwind BV"})
    other = _event("Data Days Ghent", days=25)

    result = events_mod.radar(seeker_id, campaign_id)
    ids = [e["id"] for e in result["events"]]
    assert hosted not in ids
    assert other in ids
    assert result["discretion_mode"] is True
    assert result["counts"]["excluded_by_discretion"] == 1


# ---------------------------------------------------------------------------
# FR-463: campaign export
# ---------------------------------------------------------------------------


def _second_seeker_with_overlapping_data(campaign) -> dict:
    """A second job seeker on the same companies - the FR-463 isolation case."""
    other_id = _seeker("other@example.com", "Other Person")
    profile_id = _profile(other_id)
    composite_id = _composite(other_id, profile_id)
    dream_id = _dream(other_id)
    directive_id = _directives(other_id)
    other_campaign = _campaign(other_id, directive_id, profile_id, composite_id, dream_id)
    opportunity_id = _opportunity(
        other_id,
        other_campaign,
        campaign["companies"]["acme"],
        title="SECRET ROLE OF THE OTHER SEEKER",
        required=["Python"],
        description="Only the other job seeker may see this.",
    )
    package_id = insert_row(
        "application_package",
        {
            "job_seeker_id": other_id,
            "opportunity_id": opportunity_id,
            "language": "en",
            "email_subject": "OTHER SEEKER SUBJECT",
            "status": "sent",
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )
    insert_row(
        "gap_analysis",
        {
            "job_seeker_id": other_id,
            "campaign_id": other_campaign,
            "gaps": [{"id": "skill:secret", "label": "OTHER SEEKER GAP"}],
            "created_at": utcnow(),
        },
    )
    return {
        "seeker_id": other_id,
        "campaign_id": other_campaign,
        "opportunity_id": opportunity_id,
        "package_id": package_id,
        "markers": [
            other_id,
            other_campaign,
            opportunity_id,
            package_id,
            "SECRET ROLE OF THE OTHER SEEKER",
            "OTHER SEEKER SUBJECT",
            "OTHER SEEKER GAP",
            "other@example.com",
            "Other Person",
        ],
    }


def test_json_export_contains_only_this_seekers_data(campaign):
    """FR-463 acceptance: no data of any other job seeker, anywhere."""
    other = _second_seeker_with_overlapping_data(campaign)
    gaps_mod.analyse(campaign["seeker_id"], campaign["campaign_id"], use_llm=False)

    payload = json_export.build(campaign["seeker_id"], campaign["campaign_id"])
    blob = json.dumps(payload, default=str)

    for marker in other["markers"]:
        assert marker not in blob, marker
    assert json_export.verify_isolation(payload, campaign["seeker_id"]) == []
    assert payload["counts"]["opportunities"] == 4
    assert payload["job_seeker"]["id"] == campaign["seeker_id"]
    assert payload["intelligence"]["gap_analysis"]["gaps"]

    # The companies are shared knowledge-base rows, so they are in both exports
    # - but they carry no link back to any job seeker (FR-344).
    assert any(c["name"] == "Acme Logistics" for c in payload["companies"])
    assert "job_seeker_id" not in payload["companies"][0]


def test_verify_isolation_catches_foreign_rows(campaign):
    other = _second_seeker_with_overlapping_data(campaign)
    payload = {"ranked_list": [{"id": "x", "job_seeker_id": other["seeker_id"]}]}
    offenders = json_export.verify_isolation(payload, campaign["seeker_id"])
    assert offenders == ["$.ranked_list[0].job_seeker_id"]


def test_export_package_is_a_self_contained_zip(campaign):
    other = _second_seeker_with_overlapping_data(campaign)
    gaps_mod.analyse(campaign["seeker_id"], campaign["campaign_id"], use_llm=False)
    stones_mod.propose(campaign["seeker_id"], campaign["campaign_id"], use_llm=False)

    # A document this seeker really has, to prove it is copied in.
    generated = Path(get_settings().generated_dir) / "cv.pdf"
    generated.write_bytes(b"%PDF-1.4 own cv\n")
    insert_row(
        "application_package",
        {
            "job_seeker_id": campaign["seeker_id"],
            "opportunity_id": campaign["opportunities"][0],
            "language": "en",
            "cv_pdf_path": str(generated),
            "status": "approved",
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )

    result = campaign_export.build_package(campaign["seeker_id"], campaign["campaign_id"])
    assert result.zip_path.is_file()
    assert "exports" in result.zip_path.parts

    with zipfile.ZipFile(result.zip_path) as archive:
        names = archive.namelist()
        assert "campaign.json" in names
        assert "campaign.pdf" in names
        assert "MANIFEST.json" in names
        assert any(n.startswith("documents/") for n in names)

        exported = json.loads(archive.read("campaign.json"))
        manifest = json.loads(archive.read("MANIFEST.json"))
        # FR-463: not one byte of the other seeker's data, in any member.
        for name in names:
            body = archive.read(name)
            for marker in other["markers"]:
                assert marker.encode() not in body, f"{marker} leaked into {name}"

    assert exported["campaign"]["id"] == campaign["campaign_id"]
    assert manifest["isolation_check"]["result"].startswith("passed")
    assert manifest["counts"]["documents"] == 1

    stored = campaign_export.get_export(campaign["seeker_id"], result.export_id)
    assert stored["byte_size"] > 0
    # NFR-702: exporting is an audited event.
    audit = query_one(
        "SELECT * FROM audit_event WHERE entity_id = ? AND action = 'campaign.exported'",
        (result.export_id,),
    )
    assert audit is not None


def test_export_respects_do_not_disclose_flags(campaign):
    """FR-106 / FR-463: a flagged field never reaches the package."""
    insert_row(
        "disclosure_flag",
        {
            "job_seeker_id": campaign["seeker_id"],
            "field_path": "summary",
            "do_not_disclose": 1,
            "reason": "not for third parties",
            "created_at": utcnow(),
        },
    )
    payload = json_export.build(campaign["seeker_id"], campaign["campaign_id"])
    sections = payload["profile"]["profile_version"]["sections"]
    assert "summary" not in sections
    assert payload["privacy"]["do_not_disclose_applied"] is True
    assert "summary" in payload["privacy"]["do_not_disclose_paths"]


def test_export_drops_companies_excluded_by_discretion_mode(campaign):
    """FR-385: an excluded company is not in a document handed to a coach."""
    update_row(
        "directive_set",
        campaign["directive_id"],
        {
            "discretion_mode": 1,
            "discretion_excluded_companies": [
                {"name": "Beta Freight", "reason": "current_employer"}
            ],
        },
    )
    payload = json_export.build(campaign["seeker_id"], campaign["campaign_id"])
    names = {c["name"] for c in payload["companies"]}
    assert "Beta Freight" not in names
    assert "Acme Logistics" in names
    assert payload["privacy"]["companies_excluded_by_discretion"] == 1
    assert all(
        o.get("company_name") != "Beta Freight" for o in payload["ranked_list"]
    )


# ---------------------------------------------------------------------------
# NFR-301 / FR-108: erasure reaches this slice's private tables
# ---------------------------------------------------------------------------


def test_erasure_removes_this_slices_private_rows(campaign):
    """The three private tables added for this slice cascade with the account."""
    from dreamjob.db.repositories import seekers as seekers_repo

    seeker_id = campaign["seeker_id"]
    advice = linkedin_mod.suggest(seeker_id, campaign["campaign_id"], use_llm=False)
    event_id = _event("Data Days Ghent", days=30)
    events_mod.add_to_calendar(seeker_id, event_id, campaign_id=campaign["campaign_id"])
    export = campaign_export.build_package(seeker_id, campaign["campaign_id"])
    gaps_mod.analyse(seeker_id, campaign["campaign_id"], use_llm=False)

    assert repo.get_advice(advice.advice_id, seeker_id)
    assert repo.get_interest(seeker_id, event_id)
    assert campaign_export.get_export(seeker_id, export.export_id)

    seekers_repo.erase_seeker(seeker_id)

    assert query_one("SELECT id FROM linkedin_advice WHERE job_seeker_id = ?", (seeker_id,)) is None
    assert query_one("SELECT id FROM event_interest WHERE job_seeker_id = ?", (seeker_id,)) is None
    assert query_one("SELECT id FROM campaign_export WHERE job_seeker_id = ?", (seeker_id,)) is None
    assert query_one("SELECT id FROM gap_analysis WHERE job_seeker_id = ?", (seeker_id,)) is None
    # The shared event itself is market data and survives (FR-108, FR-344).
    assert query_one("SELECT id FROM event WHERE id = ?", (event_id,)) is not None


def test_export_of_this_slices_tables_reaches_the_gdpr_export(campaign):
    """NFR-301: the three private tables added here are part of the data export."""
    from dreamjob.db.repositories import seekers as seekers_repo

    seeker_id = campaign["seeker_id"]
    linkedin_mod.suggest(seeker_id, campaign["campaign_id"], use_llm=False)
    event_id = _event("Data Days Ghent", days=30)
    events_mod.add_to_calendar(seeker_id, event_id, campaign_id=campaign["campaign_id"])
    campaign_export.build_package(seeker_id, campaign["campaign_id"])

    tables = seekers_repo.export_private_data(seeker_id)["tables"]
    for table in ("linkedin_advice", "event_interest", "campaign_export"):
        assert tables.get(table), f"{table} missing from the NFR-301 export"


# ---------------------------------------------------------------------------
# FR-461: the per-company aggregation ranks by strength and relevance
# ---------------------------------------------------------------------------


def test_company_routes_are_ranked_and_merged_on_the_best_score(db, monkeypatch):
    """FR-461: the merged list is ordered by rank, and a duplicate keeps its best rank.

    The router reads the rank off ``IntroductionRoute.as_dict()``, which
    publishes it as ``score``; a mismatch here leaves the list in collection
    order, which is exactly what FR-461 forbids.
    """
    from dreamjob.api.routers import networking as networking_router
    from dreamjob.pipeline.introductions import IntroductionRoute

    def route(name: str, strength: float, relevance: float) -> IntroductionRoute:
        return IntroductionRoute(
            member_id=name,
            name=name,
            role="CTO",
            company_name="Acme",
            linkedin_url=None,
            relationship="former_colleague",
            degree=1,
            strength=strength,
            relevance=relevance,
            rationale="worked together",
        )

    by_opportunity = {
        "o1": [route("weak", 0.10, 0.10), route("shared", 0.20, 0.20)],
        "o2": [route("strong", 0.95, 0.90), route("shared", 0.80, 0.80)],
    }
    monkeypatch.setattr(
        networking_router.opportunity_repo,
        "list_opportunities",
        lambda seeker_id, **kw: [{"id": "o1", "title": "A"}, {"id": "o2", "title": "B"}],
    )
    monkeypatch.setattr(
        networking_router.intro_mod,
        "build_routes",
        lambda seeker_id, opportunity_id, **kw: by_opportunity[opportunity_id],
    )
    monkeypatch.setattr(
        networking_router, "discretion_state", lambda *a, **kw: {"discretion_mode": False}
    )

    seeker = SimpleNamespace(id="seeker-1")
    result = networking_router.introductions_for_company(seeker, "company-1", limit=10)

    names = [r["intermediary_name"] for r in result["routes"]]
    assert names == ["strong", "shared", "weak"], names
    scores = [r["score"] for r in result["routes"]]
    assert scores == sorted(scores, reverse=True)
    # "shared" appears in both roles; the better of the two ranks is the one kept.
    shared = next(r for r in result["routes"] if r["intermediary_name"] == "shared")
    assert shared["score"] == pytest.approx(0.8)
    assert shared["opportunity_id"] == "o2"
