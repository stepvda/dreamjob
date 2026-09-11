"""The search must look where the seeker is and for what they asked (FR-142, FR-144).

Three defects, all found in the live installation, all with the same effect: a
campaign collected the whole world and ranked it.

* ``location_matches`` accepted any record it could not place, so every ATS job
  without coordinates passed regardless of its country.
* ``propose_directives`` built a location with an area but no country list, so
  the country rule could never fire.
* ``propose_directives`` read the seniority level off the career *history*, so a
  request for hands-on individual-contributor work became a manager-to-VP
  search.

Each test below pins the corrected behaviour, and the first two pin the exact
failure that produced 43,400 opportunities from 9,000 km away.
"""

from __future__ import annotations

from dreamjob.pipeline.directives import (
    DirectiveSetPayload,
    JobContentDirectives,
    LocationArea,
    LocationDirectives,
    ManagementScope,
    Seniority,
    WorkArrangement,
    WorkArrangementDirectives,
    location_matches,
    propose_directives,
    role_relevance,
)


def _directives(
    *,
    countries: list[str] | None = None,
    titles: list[str] | None = None,
    families: list[str] | None = None,
    must: list[str] | None = None,
    arrangements: list[WorkArrangement] | None = None,
) -> DirectiveSetPayload:
    return DirectiveSetPayload(
        name="test",
        job_content=JobContentDirectives(
            target_titles=titles if titles is not None else ["Backend Engineer"],
            function_families=families or [],
            must_have_skills=must or [],
        ),
        location=LocationDirectives(
            areas=[
                LocationArea(
                    label="Brussels, Belgium",
                    latitude=50.8467,
                    longitude=4.3525,
                    radius_km=35.0,
                    country_code="BE",
                )
            ],
            countries=countries if countries is not None else ["BE"],
        ),
        work_arrangement=WorkArrangementDirectives(
            arrangements=arrangements if arrangements is not None else [WorkArrangement.HYBRID]
        ),
    )


# ---------------------------------------------------------------------------
# FR-144: the country rule decides, and "unplaceable" is not "in scope"
# ---------------------------------------------------------------------------


def test_a_known_out_of_scope_country_is_rejected_without_coordinates():
    """A German job with no coordinates must not pass on being unplaceable."""
    d = _directives()
    assert location_matches(d, None, None, "DE") is False
    assert location_matches(d, None, None, "de") is False  # case-insensitive


def test_a_known_in_scope_country_is_accepted():
    assert location_matches(_directives(), None, None, "BE") is True


def test_a_record_with_neither_country_nor_coordinates_is_rejected():
    """This is the case that used to accept every job on earth."""
    assert location_matches(_directives(), None, None, None) is False


def test_the_defect_reproduces_when_the_country_list_is_empty():
    """The exact failure: an area-only directive accepted the whole world.

    ``countries`` empty made the country rule unreachable, and the unplaceable
    branch returned ``not countries`` -> True.
    """
    area_only = _directives(countries=[])
    assert location_matches(area_only, None, None, "DE") is False
    assert location_matches(area_only, None, None, "US") is False
    assert location_matches(area_only, None, None, None) is False


def test_a_nearby_record_with_coordinates_is_accepted():
    # ~5 km from the Brussels centre.
    assert location_matches(_directives(), 50.88, 4.35, None) is True


def test_a_distant_record_with_coordinates_is_rejected():
    # Paris.
    assert location_matches(_directives(), 48.8566, 2.3522, None) is False


def test_a_remote_role_passes_when_remote_is_accepted():
    """'Brussels or remote' must keep the remote roles that carry no place."""
    d = _directives(arrangements=[WorkArrangement.REMOTE])
    assert location_matches(d, None, None, None, remote=True) is True
    # ...but not when remote is not among the accepted arrangements.
    assert location_matches(_directives(), None, None, None, remote=True) is False


def test_no_location_directives_means_everything_passes():
    d = _directives(countries=[])
    d.location.areas = []
    d.location.home_location = None
    assert location_matches(d, None, None, "DE") is True


# ---------------------------------------------------------------------------
# FR-142: a role that is not the kind of work asked for is not an opportunity
# ---------------------------------------------------------------------------


def test_an_unrelated_role_is_out_of_scope():
    d = _directives()
    assert role_relevance(d, "Serveringspersonnel", "Hospitality") == "role_out_of_scope"
    assert role_relevance(d, "Childcare & Homework Tutor", "Education") == "role_out_of_scope"


def test_a_matching_title_is_in_scope():
    assert role_relevance(_directives(), "Senior Backend Engineer", "Engineering") is None


def test_a_matching_function_family_is_in_scope_even_with_an_odd_title():
    d = _directives(titles=[], families=["software engineering"])
    assert role_relevance(d, "Member of Technical Staff", "Software Engineering") is None


def test_a_must_have_skill_in_the_posting_is_in_scope():
    """An oddly titled role is still caught by the subjects it names."""
    d = _directives(must=["Python"])
    assert (
        role_relevance(d, "Founding Engineer", "Engineering", "We build with Python.") is None
    )


def test_no_titles_and_no_families_means_no_gate():
    d = _directives(titles=[], families=[])
    assert role_relevance(d, "Anything At All", "Whatever") is None


# ---------------------------------------------------------------------------
# The gate is actually applied when a vacancy becomes an opportunity
# ---------------------------------------------------------------------------


def _reject(record: dict, d: DirectiveSetPayload) -> str | None:
    from dreamjob.pipeline.opportunities import rejection_reason

    return rejection_reason(record, None, d)


def test_an_unrelated_role_never_becomes_an_opportunity():
    record = {
        "title": "Serveringspersonnel",
        "function_family": "Hospitality",
        "country": "NO",
        "work_arrangement": "onsite",
    }
    assert _reject(record, _directives()) == "role_out_of_scope"


def test_a_relevant_role_in_scope_survives_the_gate():
    record = {
        "title": "Senior Backend Engineer",
        "function_family": "Engineering",
        "country": "BE",
        "work_arrangement": "hybrid",
    }
    assert _reject(record, _directives()) is None


def test_a_relevant_role_out_of_country_is_rejected_on_location():
    record = {
        "title": "Senior Backend Engineer",
        "function_family": "Engineering",
        "country": "DE",
        "work_arrangement": "hybrid",
    }
    assert _reject(record, _directives()) == "outside_location_directives"


# ---------------------------------------------------------------------------
# FR-128 / FR-147: the directives follow the dream job, not the career history
# ---------------------------------------------------------------------------

_COMPOSITE = {
    "seniority": {
        "id": "seniority:1",
        "level": "director",
        "text": "Career reached director level; currently aiming at hands-on data and AI roles.",
    },
    "core_competencies": [{"id": "core_competencies:1", "text": "Software design"}],
    "career_trajectory": [
        {"id": "career_trajectory:1", "text": "Enterprise Strategy and Portfolio Director"}
    ],
}

_DREAM_JOB = {
    "target_roles": [{"title": "Senior Backend Engineer"}],
    "role_families": [{"family": "software engineering"}],
    "deal_breakers": [
        {"constraint": "Pure management responsibilities with no hands-on engineering"},
        {"constraint": "Business-unit leadership, pre-sales or strategy as the core mandate"},
    ],
    "implicit_preferences": [{"preference": "Hands-on; wants to write code and shape the architecture"}],
}

_PROFILE = {
    "sections": {"contact": {"location": "Brussels, Belgium"}},
}


def test_seniority_follows_the_dream_job_not_the_history():
    proposal = propose_directives(_COMPOSITE, _DREAM_JOB, _PROFILE)
    content = proposal.directives.job_content
    # The history said director; the dream job says senior. The dream job wins.
    assert content.seniority_min in (Seniority.MEDIOR, Seniority.SENIOR, Seniority.LEAD)
    assert content.seniority_max is not Seniority.VP
    assert content.seniority_max is not Seniority.DIRECTOR
    assert proposal.provenance["job_content.seniority_max"].startswith("dream_job_model")


def test_a_hands_on_request_does_not_propose_the_management_track():
    proposal = propose_directives(_COMPOSITE, _DREAM_JOB, _PROFILE)
    assert proposal.directives.job_content.management_scope is ManagementScope.INDIVIDUAL_CONTRIBUTOR
    assert "hands-on" in proposal.provenance["job_content.management_scope"]


def test_the_location_carries_a_country_so_the_country_rule_can_fire():
    proposal = propose_directives(_COMPOSITE, _DREAM_JOB, _PROFILE)
    assert proposal.directives.location.countries == ["BE"]


def test_the_history_still_decides_when_the_dream_job_is_silent():
    """The fallback is unchanged: no dream job, no contradiction to resolve."""
    proposal = propose_directives(_COMPOSITE, None, _PROFILE)
    # composite said director -> manager..vp, as before.
    assert proposal.directives.job_content.seniority_max is Seniority.VP
