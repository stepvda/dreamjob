"""Directive editor, discretion mode and geocoding maths (FR-141..149, FR-385).

Everything here runs offline: the geocoder is never called, only the pure
distance/commute model and the bundled catalogues.
"""

from __future__ import annotations

import json
import logging

import pytest
from dreamjob.config import get_settings
from dreamjob.db.connection import insert_row, utcnow
from dreamjob.db.migrator import migrate
from dreamjob.db.repositories import directives as repo
from dreamjob.pipeline import geocode as geo
from dreamjob.pipeline.directives import (
    SENIORITY_RANK,
    CommuteMode,
    CompanyReference,
    CompanyTypeDirectives,
    CompensationDirectives,
    ContractType,
    DirectiveSetPayload,
    ExcludedCompany,
    ExcludedContact,
    JobContentDirectives,
    LocationArea,
    LocationDirectives,
    Ownership,
    Seniority,
    SizeBand,
    Stage,
    Trajectory,
    WorkArrangement,
    WorkArrangementDirectives,
    allowed_source_types,
    coerce_directive_set,
    contact_exclusion_reason,
    estimate_collection,
    exclusion_reason,
    expand_synonyms,
    is_excluded,
    is_excluded_contact,
    location_matches,
    lookup_title,
    propose_directives,
    size_band_for_fte,
    suggest_titles,
    to_columns,
    vocabulary,
)
from pydantic import ValidationError

BE_JOB_BOARD = {
    "adapter_key": "vdab",
    "display_name": "VDAB",
    "source_type": "job_board",
    "coverage_countries": '["BE"]',
    "query_capabilities": '{"location_filter": true, "pagination": true,'
    ' "max_results_per_query": 100}',
    "access_method": "http",
    "rate_limit_rps": 0.5,
    "cost_per_call_eur": 0.0,
    "enabled": 1,
    "requires_ack": 0,
    "acknowledged_at": None,
}
FR_JOB_BOARD = {**BE_JOB_BOARD, "adapter_key": "apec", "coverage_countries": '["FR"]'}
BE_REGISTRY = {
    **BE_JOB_BOARD,
    "adapter_key": "kbo",
    "source_type": "registry",
    "query_capabilities": '{"company_lookup": true, "pagination": false}',
    "cost_per_call_eur": 0.01,
}


@pytest.fixture
def directives() -> DirectiveSetPayload:
    return DirectiveSetPayload(
        name="Benelux data leadership",
        job_content=JobContentDirectives(
            target_titles=["Head of Data", "Data Scientist"],
            seniority_min=Seniority.SENIOR,
            seniority_max=Seniority.DIRECTOR,
            must_have_skills=["Python"],
            keywords_to_avoid=["night shift"],
        ),
        company_type=CompanyTypeDirectives(
            exclude_companies=[CompanyReference(name="Globex")],
        ),
        location=LocationDirectives(
            areas=[LocationArea(label="Leuven", latitude=50.8792, longitude=4.7012, radius_km=30)],
            countries=["BE"],
            home_location=LocationArea(label="Leuven", latitude=50.8792, longitude=4.7012),
            max_commute_minutes=45,
            commute_mode=CommuteMode.CAR,
        ),
        work_arrangement=WorkArrangementDirectives(
            arrangements=[WorkArrangement.HYBRID],
            min_remote_days=2,
            contract_types=[ContractType.PERMANENT],
        ),
        compensation=CompensationDirectives(minimum_package=95_000),
    )


# --- FR-141/FR-146: structure and the disclosure opt-in ---------------------


def test_only_notes_to_ai_is_free_text(directives: DirectiveSetPayload) -> None:
    free_text_fields = {
        name
        for name, field in DirectiveSetPayload.model_fields.items()
        if field.annotation in (str, str | None)
    }
    assert free_text_fields == {"name", "notes_to_ai", "persona_id"}


def test_compensation_is_not_disclosed_by_default(directives: DirectiveSetPayload) -> None:
    # FR-146: figures are for filtering and scoring until the seeker opts in.
    assert directives.compensation.disclose_in_content is False
    assert CompensationDirectives().disclose_in_content is False


def test_vocabulary_is_translated(directives: DirectiveSetPayload) -> None:
    # NFR-501: every drop-down is available in English, Dutch and French.
    for locale in ("en", "nl", "fr"):
        groups = vocabulary(locale)["groups"]
        assert {"size_band", "contract_type", "commute_mode"} <= set(groups)
        assert all(option["label"] for group in groups.values() for option in group)
    assert vocabulary("nl")["groups"]["contract_type"][0]["label"] == "Vast contract"
    assert vocabulary("xx")["locale"] == "en"


# --- FR-142: title auto-complete -------------------------------------------


def test_title_autocomplete_matches_synonyms_and_ranks_canonical_first() -> None:
    assert suggest_titles("data sci")[0].canonical == "Data Scientist"
    french = suggest_titles("directeur technique", locale="fr")
    assert french and french[0].canonical == "Chief Technology Officer"
    assert french[0].label == "Directeur Technique"
    # A canonical title wins over another entry that lists it as a synonym.
    assert lookup_title("Head of Data")["canonical"] == "Head of Data"
    assert "Chief Technology Officer" in expand_synonyms(["CTO"])


def test_stored_job_content_exposes_the_query_set() -> None:
    # Other slices read the directive JSON generically; the searchable titles
    # are published under the conventional "titles" key (FR-162).
    payload = DirectiveSetPayload(
        name="q",
        job_content=JobContentDirectives(
            target_titles=["Head of Data"], title_synonyms=["Director of Data"]
        ),
    )
    stored = to_columns(payload)["job_content"]
    assert stored["target_titles"] == ["Head of Data"]
    assert stored["titles"] == ["Head of Data", "Director of Data"]


def test_size_band_mapping() -> None:
    assert size_band_for_fte(4).value == "lt10"
    assert size_band_for_fte(120).value == "b50_250"
    assert size_band_for_fte(5000).value == "gt1000"
    assert size_band_for_fte(None) is None


# --- FR-385: discretion mode ------------------------------------------------


@pytest.fixture
def discreet(directives: DirectiveSetPayload) -> DirectiveSetPayload:
    directives.discretion_mode = True
    directives.discretion_excluded_companies = [
        ExcludedCompany(name="Acme NV", domain="acme.be", reason="current_employer"),
    ]
    directives.discretion_excluded_contacts = [
        ExcludedContact(full_name="Old Boss", reason="current_colleague"),
    ]
    return directives


def test_excluded_company_and_its_group_entities(discreet: DirectiveSetPayload) -> None:
    assert exclusion_reason(discreet, {"name": "Acme NV"}) == "current_employer"
    assert exclusion_reason(discreet, "acme bvba") == "current_employer"
    # Name-prefix relation: a subsidiary leading with the parent's name.
    assert exclusion_reason(discreet, {"name": "Acme Digital Services"}) == "group_entity"
    # Shared registrable domain, including a subdomain, under another name.
    assert exclusion_reason(discreet, {"name": "Roadrunner", "domain": "acme.be"}) == "group_entity"
    assert is_excluded(discreet, {"name": "X", "domain": "https://careers.acme.be/jobs"})
    # A similar-looking but unrelated company stays in scope.
    assert not is_excluded(discreet, {"name": "Acmed Health", "domain": "acmed.com"})
    assert not is_excluded(discreet, {"name": "Initech", "domain": "initech.com"})


def test_user_exclusions_apply_without_discretion_mode(discreet: DirectiveSetPayload) -> None:
    # FR-143 exclusions are always in force; FR-385 ones only in discretion mode.
    off = discreet.model_copy(deep=True)
    off.discretion_mode = False
    assert exclusion_reason(off, {"name": "Globex"}) == "user_excluded"
    assert exclusion_reason(off, {"name": "Acme NV"}) is None
    assert exclusion_reason(discreet, {"name": "Acme NV"}) == "current_employer"
    assert contact_exclusion_reason(off, {"email": "ann@acme.be"}) is None


def test_contact_exclusion(discreet: DirectiveSetPayload) -> None:
    recruiter = {
        "full_name": "Jan Peeters",
        "email": "jan@acme.be",
        "role_title": "Talent Acquisition Lead",
    }
    colleague = {"full_name": "Ann Smith", "email": "ann@acme.be", "role_title": "Controller"}
    outsider = {"full_name": "Bo Lee", "email": "bo@initech.com", "role_title": "CTO"}

    assert contact_exclusion_reason(discreet, recruiter) == "employer_recruiter"
    assert contact_exclusion_reason(discreet, colleague) == "current_colleague"
    assert is_excluded_contact(discreet, {"full_name": "Old Boss"})
    assert not is_excluded_contact(discreet, outsider)
    # The employer may be passed in when the caller already resolved it.
    assert is_excluded_contact(discreet, outsider, {"name": "Acme NV"})


def _as_row(payload: DirectiveSetPayload, **extra: object) -> dict:
    """The shape a repository read returns: JSON columns as text, flags as ints."""
    columns = {
        key: (value if isinstance(value, (int, str)) or value is None else json.dumps(value))
        for key, value in to_columns(payload).items()
    }
    return {"id": "d1", "job_seeker_id": "js1", "created_at": utcnow(), **columns, **extra}


def test_helpers_accept_a_database_row(discreet: DirectiveSetPayload) -> None:
    row = _as_row(discreet, version=3)
    assert coerce_directive_set(row).version == 3
    assert is_excluded(row, {"name": "Acme Digital Services"})
    assert is_excluded_contact(row, {"email": "someone@acme.be"})


# --- Legacy stored values must never 500 a read (FR-143, NFR-501) -----------


def _legacy_row(**company_type_overrides: object) -> dict:
    """A directive row as stored before the canonical vocabulary existed.

    The observed offending values are the human size bands "50-250" and
    ">1000"; the other groups carry label spellings too, to prove the whole
    persisted structure is coerced and not just the field that was reported.
    """
    company_type: dict[str, object] = {
        "size_bands": ["50-250", ">1000"],
        "stages": ["Scale-up"],
        "trajectories": ["Growing"],
        "ownerships": ["PE-backed"],
    }
    company_type.update(company_type_overrides)
    return {
        "id": "d-legacy",
        "job_seeker_id": "js1",
        "name": "Legacy set",
        "version": 2,
        "created_at": utcnow(),
        "job_content": json.dumps(
            {
                "target_titles": ["Head of Data"],
                "seniority_min": "Senior",
                "seniority_max": "Director",
            }
        ),
        "company_type": json.dumps(company_type),
        "location": json.dumps({"commute_mode": "Car"}),
        "work_arrangement": json.dumps(
            {"arrangements": ["Hybrid"], "contract_types": ["Permanent"]}
        ),
        "compensation": json.dumps({}),
        "spontaneous_only": 0,
        "discretion_mode": 0,
        "discretion_excluded_companies": json.dumps([]),
        "discretion_excluded_contacts": json.dumps([]),
    }


def test_legacy_labels_coerce_to_canonical_values() -> None:
    result = coerce_directive_set(_legacy_row())
    assert result.company_type.size_bands == [SizeBand.B50_250, SizeBand.GT1000]
    assert result.company_type.stages == [Stage.SCALEUP]
    assert result.company_type.trajectories == [Trajectory.GROWING]
    assert result.company_type.ownerships == [Ownership.PE_BACKED]
    assert result.job_content.seniority_min is Seniority.SENIOR
    assert result.job_content.seniority_max is Seniority.DIRECTOR
    assert result.work_arrangement.arrangements == [WorkArrangement.HYBRID]
    assert result.work_arrangement.contract_types == [ContractType.PERMANENT]
    assert result.location.commute_mode is CommuteMode.CAR


def test_numeric_ranges_are_parsed_into_the_matching_bucket() -> None:
    row = _legacy_row(size_bands=["10-50", "250 - 1000", "1000+", "<10"])
    assert coerce_directive_set(row).company_type.size_bands == [
        SizeBand.B10_50,
        SizeBand.B250_1000,
        SizeBand.GT1000,
        SizeBand.LT10,
    ]
    # A thousands separator is read as one number, not two.
    thousands = _legacy_row(size_bands=["1.000"])
    assert coerce_directive_set(thousands).company_type.size_bands == [SizeBand.B250_1000]


def test_unknown_enum_values_are_dropped_not_raised(caplog) -> None:
    row = _legacy_row(size_bands=["50-250", "enormous"], stages=["Scale-up", "mystery"])
    with caplog.at_level(logging.WARNING):
        result = coerce_directive_set(row)
    assert result.company_type.size_bands == [SizeBand.B50_250]
    assert result.company_type.stages == [Stage.SCALEUP]
    assert "enormous" in caplog.text
    assert "mystery" in caplog.text


def test_a_canonical_set_is_unchanged_by_coercion() -> None:
    payload = DirectiveSetPayload(
        name="Canonical",
        job_content=JobContentDirectives(
            seniority_min=Seniority.SENIOR, seniority_max=Seniority.DIRECTOR
        ),
        company_type=CompanyTypeDirectives(
            size_bands=[SizeBand.B50_250, SizeBand.GT1000],
            stages=[Stage.SCALEUP],
            ownerships=[Ownership.PE_BACKED],
        ),
        work_arrangement=WorkArrangementDirectives(
            arrangements=[WorkArrangement.HYBRID], contract_types=[ContractType.PERMANENT]
        ),
    )
    result = coerce_directive_set(_as_row(payload))
    assert result.model_dump(exclude={"id", "job_seeker_id", "version", "created_at"}) == (
        payload.model_dump()
    )


def test_structural_errors_still_raise() -> None:
    row = _legacy_row()
    row["company_type"] = json.dumps({"size_bands": "50-250"})  # a scalar, not a list
    with pytest.raises(ValidationError):
        coerce_directive_set(row)


def test_export_shaped_payload_with_legacy_bands_does_not_raise() -> None:
    """The exact shape that 500'd the networking export: a stale stored row."""
    row = _legacy_row()
    row["discretion_mode"] = 1
    row["discretion_excluded_companies"] = json.dumps(
        [{"name": "Acme NV", "reason": "Current employer"}]
    )
    assert exclusion_reason(row, {"name": "Acme NV"}) == "current_employer"
    assert coerce_directive_set(row).company_type.size_bands == [SizeBand.B50_250, SizeBand.GT1000]


# --- FR-149: spontaneous applications ---------------------------------------


def test_spontaneous_only_drops_vacancy_sources(directives: DirectiveSetPayload) -> None:
    assert "job_board" in allowed_source_types(directives)
    directives.spontaneous_only = True
    allowed = allowed_source_types(directives)
    assert "job_board" not in allowed and "ats" not in allowed
    assert {"website", "registry", "directory"} <= allowed

    estimate = estimate_collection(directives, [BE_JOB_BOARD, BE_REGISTRY])
    assert [s.adapter_key for s in estimate.sources] == ["kbo"]
    assert {"adapter_key": "vdab", "reason": "spontaneous_only"} in estimate.skipped
    assert any("FR-149" in note for note in estimate.notes)


# --- FR-147: estimate and proposal ------------------------------------------


def test_estimate_respects_coverage_and_counts_pages(directives: DirectiveSetPayload) -> None:
    estimate = estimate_collection(directives, [BE_JOB_BOARD, FR_JOB_BOARD, BE_REGISTRY])
    assert {"adapter_key": "apec", "reason": "outside_coverage"} in estimate.skipped
    assert estimate.source_count == 2
    assert estimate.query_count > 0
    assert estimate.total_pages >= estimate.query_count
    assert estimate.total_seconds > 0
    assert estimate.total_cost_eur == pytest.approx(0.01)
    assert estimate_collection(directives, []).notes


def test_proposal_is_prefilled_from_the_profile() -> None:
    composite = {
        "seniority": '"Senior data scientist moving towards a lead role"',
        "career_trajectory": '["Data Scientist", "Senior Data Scientist"]',
        "core_competencies": '["Python", "Machine learning"]',
        "adjacent_competencies": '["Cloud architecture"]',
        "domains": '["Manufacturing"]',
        "inferred_preferences": '{"working": "prefers hybrid working"}',
    }
    dream = {"target_roles": '["Head of Data"]'}
    profile = {"sections": '{"basics": {"location": "Leuven, Belgium"}}'}

    proposal = propose_directives(composite, dream, profile)
    content = proposal.directives.job_content
    assert "Head of Data" in content.target_titles
    assert content.must_have_skills == ["Python", "Machine learning"]
    assert content.seniority_min and content.seniority_max
    assert proposal.directives.location.home_location.label == "Leuven, Belgium"
    assert WorkArrangement.HYBRID in proposal.directives.work_arrangement.arrangements
    # FR-146 is never guessed, and never disclosed by default.
    assert proposal.directives.compensation.minimum_package is None
    assert proposal.directives.compensation.disclose_in_content is False
    assert "compensation.minimum_package" in proposal.unresolved
    assert proposal.provenance["job_content.must_have_skills"].startswith("composite_profile")


def test_proposal_keeps_a_principal_profile_at_its_own_level() -> None:
    """Regression: PRINCIPAL sits off the seniority ladder (it shares MANAGER's rank).

    Reading it as rung zero proposed an intern-to-junior range for one of the
    most senior profiles there is, and the FR-147 pre-fill is what the seeker
    launches from.
    """
    composite = {
        "seniority": '"Principal engineer and architect"',
        "career_trajectory": '["Principal Data Architect"]',
    }
    content = propose_directives(composite, None, None).directives.job_content
    assert SENIORITY_RANK[content.seniority_min] >= SENIORITY_RANK[Seniority.LEAD]
    assert SENIORITY_RANK[content.seniority_max] > SENIORITY_RANK[content.seniority_min]


def test_prohibited_sources_are_not_counted_in_the_estimate(
    directives: DirectiveSetPayload,
) -> None:
    """IR-101: the planner never selects them, so the estimate must not promise them."""
    prohibited = {**BE_JOB_BOARD, "adapter_key": "scraped", "tos_status": "prohibited"}
    acknowledged = {
        **BE_JOB_BOARD,
        "adapter_key": "scraped_ack",
        "tos_status": "prohibited",
        "acknowledged_at": utcnow(),
    }
    estimate = estimate_collection(directives, [BE_JOB_BOARD, prohibited, acknowledged])
    assert {"adapter_key": "scraped", "reason": "tos_prohibited"} in estimate.skipped
    assert {e.adapter_key for e in estimate.sources} == {"vdab", "scraped_ack"}


def test_vocabulary_carries_the_slider_bounds() -> None:
    """FR-141: the numeric controls are sliders, bounded by the model's own limits."""
    ranges = vocabulary("fr")["ranges"]
    assert ranges["max_commute_minutes"]["max"] == 600
    assert ranges["min_remote_days"]["max"] == 7
    assert LocationArea(label="x", radius_km=ranges["radius_km"]["max"]).radius_km


def test_proposal_survives_an_empty_profile() -> None:
    proposal = propose_directives(None, None, None)
    assert proposal.directives.job_content.target_titles == []
    assert "job_content.target_titles" in proposal.unresolved


# --- FR-144: location filter and commute model ------------------------------


def test_location_post_filter(directives: DirectiveSetPayload) -> None:
    assert location_matches(directives, 50.88, 4.70, "BE")           # in the area
    assert location_matches(directives, 50.8467, 4.3525, "BE")       # Brussels, in radius
    assert not location_matches(directives, 48.8566, 2.3522, "FR")   # Paris, wrong country
    assert location_matches(directives, None, None, "BE")            # coarse record, right country
    assert location_matches(DirectiveSetPayload(name="anywhere"), 1.0, 1.0, "SG")


def test_commute_model_is_monotonic_and_ordered() -> None:
    for mode in ("walk", "bike", "public_transport", "car"):
        assert geo.commute_minutes(5, mode) < geo.commute_minutes(25, mode)
    assert geo.commute_minutes(10, "walk") > geo.commute_minutes(10, "bike")
    assert geo.commute_minutes(10, "bike") > geo.commute_minutes(10, "car")
    # A 45-minute car commute is a plausible commuter-belt radius.
    assert 25 < geo.max_radius_km(45, "car") < 45
    assert geo.max_radius_km(5, "car") == 0.0
    # The inverse agrees with the forward model.
    radius = geo.max_radius_km(30, "public_transport")
    assert geo.commute_minutes(radius, "public_transport") == pytest.approx(30, abs=0.1)
    assert geo.within_commute((50.8792, 4.7012), (50.8467, 4.3525), 45, "car")


def test_haversine_against_a_known_distance() -> None:
    # Leuven to Brussels is about 25 km as the crow flies.
    assert geo.haversine_km(50.8792, 4.7012, 50.8467, 4.3525) == pytest.approx(24.7, abs=0.5)
    assert geo.haversine_km(50.0, 4.0, 50.0, 4.0) == 0.0


def test_geocoder_url_is_configured() -> None:
    assert get_settings().geocoder_url.startswith("http")


# --- FR-148: saved, named, versioned, reusable ------------------------------


@pytest.fixture
def db(tmp_path, monkeypatch) -> str:
    """An isolated migrated database with one job seeker in it."""
    monkeypatch.setenv("DREAMJOB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DREAMJOB_DB_PATH", str(tmp_path / "dreamjob.db"))
    get_settings.cache_clear()
    migrate()
    seeker_id = insert_row(
        "job_seeker",
        {
            "email": "seeker@example.test",
            "display_name": "Test Seeker",
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )
    yield seeker_id
    get_settings.cache_clear()


def test_directive_sets_are_versioned_and_isolated(
    db: str, directives: DirectiveSetPayload
) -> None:
    first = repo.create(db, directives)
    stored = repo.get(first, db)
    assert stored["version"] == 1
    assert stored["job_content"]["target_titles"] == ["Head of Data", "Data Scientist"]
    assert stored["compensation"]["disclose_in_content"] is False

    directives.job_content.target_titles = ["Chief Data Officer"]
    second = repo.create(db, directives)
    assert repo.get(second, db)["version"] == 2

    # The latest version of each name is what the editor lists (FR-148).
    assert [s["id"] for s in repo.list_for_seeker(db)] == [second]
    assert len(repo.list_for_seeker(db, all_versions=True)) == 2
    assert repo.get_latest_by_name(db, directives.name)["id"] == second

    # FR-344: another job seeker cannot read it.
    assert repo.get(first, "someone-else") is None


def test_renaming_a_set_moves_it_into_the_new_name_version_line(
    db: str, directives: DirectiveSetPayload
) -> None:
    """Regression: a rename kept its old number and collided with that name (FR-148).

    The renamed set then tied on MAX(version) and vanished from the
    latest-per-name listing the editor shows.
    """
    repo.create(db, directives)
    repo.create(db, directives)
    other = repo.create(db, DirectiveSetPayload(name="Side project"))

    renamed = repo.update_in_place(
        other, db, DirectiveSetPayload(name=directives.name)
    )
    assert renamed["version"] == 3
    assert [s["id"] for s in repo.list_for_seeker(db)] == [other]


def test_a_used_directive_set_cannot_be_deleted(db: str, directives: DirectiveSetPayload) -> None:
    directive_set_id = repo.create(db, directives)
    assert repo.campaign_usage(directive_set_id, db) == 0
    assert repo.delete(directive_set_id, db) is True

    directive_set_id = repo.create(db, directives)
    profile_version_id = insert_row(
        "profile_version",
        {"job_seeker_id": db, "version": 1, "sections": {}, "created_at": utcnow()},
    )
    insert_row(
        "campaign",
        {
            "job_seeker_id": db,
            "directive_set_id": directive_set_id,
            "profile_version_id": profile_version_id,
            "name": "Autumn search",
            "created_at": utcnow(),
        },
    )
    assert repo.campaign_usage(directive_set_id, db) == 1
    with pytest.raises(ValueError):
        repo.delete(directive_set_id, db)


def test_discretion_flags_round_trip(db: str, discreet: DirectiveSetPayload) -> None:
    directive_set_id = repo.create(db, discreet)
    row = repo.get(directive_set_id, db)
    assert row["discretion_mode"] is True
    assert row["discretion_excluded_companies"][0]["reason"] == "current_employer"
    # The stored row is what other slices are handed, and it still excludes.
    assert is_excluded(row, {"name": "Acme Digital Services"})

    updated = repo.update_columns(
        directive_set_id, db, {"discretion_mode": 0, "discretion_excluded_companies": []}
    )
    assert updated["discretion_mode"] is False
    assert not is_excluded(updated, {"name": "Acme NV"})


# --- API wiring (FR-141, FR-147, FR-385) ------------------------------------


@pytest.fixture
def client(db: str):
    """The directives router alone, with authentication stubbed out."""
    from dreamjob.api.deps import CurrentSeeker, current_seeker
    from dreamjob.api.routers import directives as router_module
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(router_module.router, prefix="/api/directives")
    app.dependency_overrides[current_seeker] = lambda: CurrentSeeker(
        id=db, email="seeker@example.test", display_name="Test", is_admin=False, locale="nl"
    )
    return TestClient(app)


def test_a_new_version_stays_on_its_name_line(client) -> None:
    """FR-148: POST /{id}/versions versions the set; /duplicate is what renames it."""
    created = client.post("/api/directives/", json={"name": "Autumn search"}).json()
    versioned = client.post(
        f"/api/directives/{created['id']}/versions",
        json={"job_content": {"target_titles": ["Chief Data Officer"]}},
    ).json()
    assert (versioned["name"], versioned["version"]) == ("Autumn search", 2)
    assert len(client.get(f"/api/directives/{created['id']}/versions").json()) == 2

    duplicated = client.post(
        f"/api/directives/{created['id']}/duplicate", json={"name": "Winter search"}
    ).json()
    assert (duplicated["name"], duplicated["version"]) == ("Winter search", 1)


def test_editor_endpoints(client, directives: DirectiveSetPayload) -> None:
    vocab = client.get("/api/directives/vocabulary", params={"locale": "fr"})
    assert vocab.status_code == 200
    assert vocab.json()["groups"]["stage"][0]["label"] == "Start-up"

    titles = client.get("/api/directives/titles", params={"q": "machine learning"})
    assert titles.json()[0]["canonical"] == "Machine Learning Engineer"

    created = client.post("/api/directives/", json=directives.model_dump(mode="json"))
    assert created.status_code == 201
    body = created.json()
    assert body["version"] == 1 and body["compensation"]["disclose_in_content"] is False

    assert client.get(f"/api/directives/{body['id']}").json()["name"] == directives.name
    assert client.get("/api/directives/missing").status_code == 404

    estimate = client.post(
        "/api/directives/estimate", json={"directive_set_id": body["id"]}
    )
    assert estimate.status_code == 200
    assert "source_count" in estimate.json()

    commute = client.post(
        "/api/directives/locations/commute",
        json={
            "origin": {"latitude": 50.8792, "longitude": 4.7012},
            "destination": {"latitude": 50.8467, "longitude": 4.3525},
            "mode": "car",
        },
    )
    assert 20 < commute.json()["minutes"] < 60

    discretion = client.put(
        f"/api/directives/{body['id']}/discretion",
        json={
            "discretion_mode": True,
            "current_employer": {"name": "Acme NV", "domain": "acme.be"},
        },
    )
    assert discretion.json()["discretion_mode"] is True
    check = client.post(
        f"/api/directives/{body['id']}/discretion/check",
        json={"company": {"name": "Acme Digital Services"}},
    )
    assert check.json()["company"] == {"excluded": True, "reason": "group_entity"}
