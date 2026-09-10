"""What becomes a search term (FR-142, FR-162).

A structured entry names the thing it is in one field and spends the rest on
provenance: why it was chosen, the quote it was inferred from, the word
"inferred" itself, its own id.  Two layers flattened all of it indiscriminately,
and the result was searched for:

    https://www.linkedin.com/search/results/people?keywords=inferred
    https://www.linkedin.com/search/results/people?keywords=career_trajectory:1

Four of the five LinkedIn people searches a real campaign planned were junk of
this kind, while the genuine titles - AI Engineer, Machine Learning Engineer,
Principal Engineer (Hands-on) - sat at positions 8 to 21 and were never
reached.  Each junk search costs a page load against an account the product
warns can be restricted, so this is not only wasted effort.

The same reasoning already guards locations, where ``planning._PLACE_KEYS``
exists so a board is never searched for "jobs in car".
"""

from __future__ import annotations

from dreamjob.pipeline import directives as dmod
from dreamjob.pipeline import planning

#: The shape the dream-job model really stores (checked against live data).
TARGET_ROLES = [
    {
        "title": "Full-Stack Engineer (AI/Data Applications)",
        "seniority": "senior individual contributor",
        "priority": 1,
        "rationale": "Closest match to the request to return to hands-on design and build.",
        "source": "inferred",
        "quote": "designing and building software properly",
    },
    {
        "title": "AI Application Engineer",
        "seniority": "senior individual contributor",
        "priority": 2,
        "rationale": "Captures the explicit wish to move into the AI and data side.",
        "source": "inferred",
        "quote": "the AI and data side of it",
    },
]

#: Biography, not titles: ``{id, text}`` where the text is a sentence.
CAREER_TRAJECTORY = [
    {"id": "career_trajectory:1", "text": "Joined Fujitsu in Brussels as Senior Consultant."},
    {"id": "career_trajectory:2", "text": "Moved to NGA Human Resources as ECM Product Manager."},
]


# ---------------------------------------------------------------------------
# planning: the keyword scrape
# ---------------------------------------------------------------------------


def test_a_structured_entry_is_read_through_the_field_that_names_it():
    out: list[str] = []
    planning._flatten_strings(TARGET_ROLES, out)
    assert out == ["Full-Stack Engineer (AI/Data Applications)", "AI Application Engineer"]
    # Specifically: none of the provenance came with it.
    assert "inferred" not in out
    assert not any(t.startswith("Closest match") for t in out)


def test_a_plain_mapping_is_still_walked_whole():
    """No naming field means nothing to prefer, so the old behaviour stands."""
    out: list[str] = []
    planning._flatten_strings({"a": "Head of Data", "b": ["Data Engineer"]}, out)
    assert sorted(out) == ["Data Engineer", "Head of Data"]


def test_example_titles_survive_alongside_the_family_name():
    out: list[str] = []
    planning._flatten_strings(
        [{"family": "software engineering",
          "example_titles": ["Full-Stack Engineer", "Software Engineer"],
          "confidence": 0.95}],
        out,
    )
    assert out == ["software engineering", "Full-Stack Engineer", "Software Engineer"]
    assert "0.95" not in out


def test_sentences_and_entry_ids_are_not_search_terms():
    assert planning._is_search_term("Head of Data")
    assert planning._is_search_term("Principal Engineer (Hands-on)")
    assert not planning._is_search_term("career_trajectory:1")
    assert not planning._is_search_term("core_competencies:12")
    assert not planning._is_search_term(
        "Design and build software hands-on, using careful object-oriented design"
    )
    assert not planning._is_search_term("Captures the explicit wish to move into AI.")
    assert not planning._is_search_term("")


def test_campaign_keywords_drops_the_junk_a_polluted_directive_set_carries():
    """Defence in depth: even stored directives full of prose cannot search with it."""
    directives = {
        "job_content": {
            "titles": [
                "Full-Stack Engineer (AI/Data Applications)",
                "career_trajectory:1",
                "Closest match to the request to return to hands-on design and build.",
            ]
        }
    }
    terms = planning.campaign_keywords(directives, None, None)
    assert terms == ["Full-Stack Engineer (AI/Data Applications)"]


def test_campaign_keywords_prefers_real_titles_from_the_dream_model():
    dream = {"target_roles": TARGET_ROLES}
    terms = planning.campaign_keywords({}, dream, None)
    assert terms[:2] == [
        "Full-Stack Engineer (AI/Data Applications)",
        "AI Application Engineer",
    ], "the titles LinkedIn will actually be searched for must come first"


# ---------------------------------------------------------------------------
# directives: the proposal that writes the directive set
# ---------------------------------------------------------------------------


def test_proposed_roles_come_from_the_naming_field_only():
    roles = dmod._role_names(TARGET_ROLES)
    assert roles == ["Full-Stack Engineer (AI/Data Applications)", "AI Application Engineer"]


def test_a_bare_list_of_titles_is_still_read():
    """``target_roles`` is stored either way; both are names."""
    assert dmod._role_names(["Head of Data", "Data Engineer"]) == [
        "Head of Data", "Data Engineer",
    ]


def test_an_unfamiliar_entry_shape_is_read_whole_rather_than_dropped():
    assert dmod._role_names([{"unexpected": "Head of Data"}]) == ["Head of Data"]


def test_biography_prose_is_not_proposed_as_a_target_title():
    """career_trajectory text is a sentence about a job held, not the name of one."""
    mined = [
        text
        for text in dmod._texts(CAREER_TRAJECTORY)
        if dmod.lookup_title(text)
    ]
    assert mined == []
