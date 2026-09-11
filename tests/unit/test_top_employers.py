"""Notable employers are planned first, and only as a hint (FR-162, FR-164).

The knowledge base is shared and global, so without an ordering the planner
spends a Brussels search's budget on whichever company was touched most
recently anywhere in the world. These tests pin the ordering, and pin the two
limits on it: it reorders work, it never invents facts, and a country the list
does not cover is left alone.
"""

from __future__ import annotations

from dreamjob.pipeline import top_employers as te


def test_a_listed_employer_is_ranked():
    ranks = te.rank_index(["BE"])
    assert te.priority_of({"name": "Aikido Security"}, ranks) is not None
    assert te.priority_of({"domain": "aikido.dev"}, ranks) is not None


def test_an_unlisted_employer_has_no_rank():
    ranks = te.rank_index(["BE"])
    assert te.priority_of({"name": "Some Local Bakery"}, ranks) is None


def test_prioritise_moves_listed_employers_ahead_and_keeps_the_rest_in_order():
    companies = [
        {"name": "Some Local Bakery"},
        {"name": "Aikido Security"},
        {"name": "Another Unknown BVBA"},
        {"name": "Materialise"},
    ]
    ordered = te.prioritise(companies, ["BE"])
    assert [c["name"] for c in ordered[:2]] == ["Aikido Security", "Materialise"]
    # The unlisted pair keeps its incoming order.
    assert [c["name"] for c in ordered[2:]] == ["Some Local Bakery", "Another Unknown BVBA"]


def test_a_country_the_list_does_not_cover_is_left_untouched():
    """No ranking for the country means the incoming order stands."""
    companies = [{"name": "A"}, {"name": "B"}]
    assert te.prioritise(companies, ["ZZ"]) == companies


def test_no_countries_means_no_reordering():
    companies = [{"name": "Aikido Security"}, {"name": "B"}]
    assert te.prioritise(companies, []) == companies
    assert te.prioritise(companies, None) == companies


def test_a_ranked_company_is_a_hint_not_a_fact():
    """The seeds carry a name, a domain and a source - nothing researched."""
    seeds = te.seeds(["BE"], limit=5)
    assert seeds
    for seed in seeds:
        assert set(seed) <= {"name", "domain", "country", "top_employer_source"}
        assert seed["name"]
        assert "business_summary" not in seed
        assert "size_band" not in seed


def test_the_list_names_where_it_came_from():
    """Every entry is citable, which is why it is curated rather than scraped."""
    for seed in te.seeds(["BE"], limit=100):
        assert seed["top_employer_source"]


def test_seeds_respect_the_limit():
    assert len(te.seeds(["BE"], limit=3)) == 3
