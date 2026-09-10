"""Telling one plan item from another (FR-162, FR-163).

The collection screen draws one row per ``source_plan_item`` and titles it with
the adapter's ``display_name``.  A plan holds 2,417 Personio boards, 251 EURES
partitions and 20 identical Actiris searches, so the screen a person watches a
four-hour run on is a list of rows reading "Personio", "Personio", "Personio" -
which is the complaint this module answers.

What separates two items of one adapter is inside ``native_query``, and every
adapter family writes a different shape there: an ATS item carries a board
slug, a register sweep carries a NUTS region and a NACE section, a board search
carries keywords and a location, a crawl carries a URL.  So the label is keyed
on *the shape of the query* and not on the adapter key: an adapter this
function has never seen still gets a name from whichever rule its query
answers, and there is no table of adapters to keep up to date.

Every ``native_query`` below is copied verbatim from a row of the installed
database.  Measured over all 57,250 ``source_plan_item`` rows in it: 0.23% fall
through to the digest fallback and every one of those is an ATS item with an
empty ``board_slugs``, which genuinely names no target; label length is median
10, p95 27, hard cap 48; 4.24% share a label with a sibling of the same
campaign and adapter and 91.3% of those resolve to the identical ``target_key``,
which is the same fetch planned twice and is what the screen collapses.

Pure functions over a dict.  Nothing here opens a database.
"""

from __future__ import annotations

import pytest
from dreamjob.pipeline.planning import (
    LABEL_MAX_CHARS,
    plan_item_label,
    short_source_name,
    target_key,
)

# ---------------------------------------------------------------------------
# One real row per adapter family in the installed database.
# ---------------------------------------------------------------------------

#: The campaign's scoring vocabulary, byte-identical on every sibling item of
#: the campaign that produced these rows.  It is the reason a keyword list can
#: never be a label on its own for an ATS item: all 2,417 carry this one.
KEYWORD_BAG = [
    "Full-Stack Engineer (AI/Data Applications)",
    "senior individual contributor",
    "inferred",
    "designing and building software properly",
    "career_trajectory:1",
    "career_trajectory:2",
]

REAL_ROWS: list[tuple[str, dict, str, str]] = [
    (
        "ats.personio",
        {"slug": "credium", "company_id": "fd11bebe45f64627ba40c6ddd157fddd",
         "company_name": "credium GmbH", "max_records": 500, "keywords": KEYWORD_BAG,
         "title_filter": False},
        "credium GmbH",
        "personio/credium · 6 keywords",
    ),
    (
        "ats.teamtailor",
        {"slug": "nofence", "company_id": "47aa76e5d02e4e2fab41098ea37082be",
         "company_name": "Nofence", "max_records": 500, "keywords": KEYWORD_BAG,
         "title_filter": False},
        "Nofence",
        "teamtailor/nofence · 6 keywords",
    ),
    (
        "ats.greenhouse",
        {"slug": "ok-board", "company_id": "fad95ef6a8d64debb80d4c09da076947",
         "company_name": "ok-board", "max_records": 500, "keywords": KEYWORD_BAG,
         "title_filter": False},
        "ok-board",
        "greenhouse/ok-board · 6 keywords",
    ),
    (
        # No company was ever resolved for this board: the slug is all there is,
        # and it is enough.  ``discovered_via`` is not part of the name.
        "ats.ashby",
        {"slug": "9-mothers", "company_id": None, "company_name": None, "max_records": 500,
         "keywords": ["Staff Backend Engineer", "Principal Engineer (Event-Driven Systems)",
                      "Platform Architect", "Senior Backend Engineer", "Software Engineer",
                      "Platform Engineer"],
         "title_filter": False, "discovered_via": "hackernews"},
        "9-mothers",
        "ashby/9-mothers · 6 keywords",
    ),
    (
        # Workday: seven rows of this campaign share DELOITTE's board and differ
        # only in ``search_text``.  The company alone would label all seven the
        # same, so the scalar search this item alone issues is appended.
        "ats.workday",
        {"slug": "deloitteie.wd3.myworkdayjobs.com/Experienced_Professionals",
         "company_id": "b896c9c758e5471fbb14f74ac76cc3aa", "company_name": "DELOITTE",
         "search_text": "Full-Stack Engineer (AI/Data Applications)", "max_records": 500,
         "keywords": KEYWORD_BAG, "title_filter": False},
        "DELOITTE · Full-Stack Engineer (AI/Dat…",
        "workday/deloitteie.wd3.myworkdayjobs.com/Experienced_Professionals · 6 keywords",
    ),
    (
        # One partition of the EURES sweep: region x sector x period.  251 of
        # these were planned, and the region and section are the only things
        # that differ between them.
        "board.eures",
        {"country_codes": ["BE"], "pages": 10, "results_per_page": 50, "language": "en",
         "fetch_details": False, "publication_period": "LAST_MONTH", "nuts_codes": ["BE1"],
         "nace_section": "A"},
        "BE1 · NACE A",
        "last month · en",
    ),
    (
        # Post-fix Actiris: one item for the whole search, paged by the
        # pipeline.  See ``test_the_pre_fix_actiris_shape_still_labels`` for the
        # twenty-row shape this replaced.
        "board.actiris",
        {"language": "nl", "offers_per_page": 50, "max_age_days": 30,
         "keywords": ["full-stack developer", "software engineer", "AI engineer",
                      "data engineer", "application developer"],
         "title_filter": False},
        "full-stack developer +4",
        "nl",
    ),
    (
        "board.arbeitnow",
        {"pages": 1, "results_per_page": 100,
         "keywords": ["Full-Stack Engineer", "AI Engineer", "Software Engineer",
                      "Python FastAPI React", "Machine Learning Engineer"],
         "title_filter": False},
        "Full-Stack Engineer +4",
        "",
    ),
    (
        "board.jobat",
        {"queries": ["Full-Stack Engineer", "AI Engineer", "Software Engineer",
                     "Python Developer", "Data Scientist"],
         "locations": ["Brussels"], "pages": 3, "max_records": 100},
        "Full-Stack Engineer +4 · Brussels",
        "",
    ),
    (
        # A register lookup: the subject is a company, nested under its own key.
        "registry.kbo",
        {"company": {"id": "fad95ef6a8d64debb80d4c09da076947", "name": "ok-board",
                     "legal_id": None, "legal_id_type": None, "vat_number": None,
                     "domain": None, "country": None, "jurisdiction": None},
         "years": 1},
        "ok-board",
        "",
    ),
    (
        "website.crawl",
        {"url": "https://stepvda.net", "company_id": "stepvda.net", "max_pages": 1,
         "max_depth": 1},
        "stepvda.net",
        "",
    ),
    (
        "news.rss",
        {"url": "https://multiverse.com", "company_id": "70b8fbeec31b472ba5097a54432d793e",
         "feeds": None},
        "multiverse.com",
        "",
    ),
    (
        "directory.opencorporates",
        {"query": "software development", "country": "BE", "countries": ["BE"]},
        "software development · BE",
        "",
    ),
    (
        # A statistical sweep names no target at all beyond the country, so the
        # occupations it asks about go to the muted line.
        "compensation.eurostat_ses",
        {"countries": ["BE"],
         "occupations": ["software developer", "computer programmer", "data scientist",
                         "information and communications technology professional"]},
        "BE",
        "4 occupations",
    ),
    (
        # An ATS item discovery never found a board for.  Nothing in the query
        # names a target, so the fetch ledger's own key is used.  That key covers
        # ``countries``, so the same vendor swept for BE and for NL is two
        # targets and two names rather than one of each.
        "ats.unknownvendor",
        {"vendor": "ashby", "board_slugs": [], "keywords": [], "countries": ["BE"]},
        "#07e42d80",
        "",
    ),
]


@pytest.mark.parametrize(
    ("adapter_key", "native_query", "label", "detail"),
    REAL_ROWS,
    ids=[row[0] for row in REAL_ROWS],
)
def test_each_query_shape_is_named_by_what_separates_it(
    adapter_key: str, native_query: dict, label: str, detail: str
) -> None:
    """FR-162: fifteen real rows, fifteen names, none of them the adapter's."""
    assert plan_item_label(adapter_key, native_query) == (label, detail)


def test_no_label_is_ever_empty_or_longer_than_the_row_can_hold() -> None:
    """A blank label leaves the row with nothing but the adapter name again."""
    for adapter_key, native_query, _label, _detail in REAL_ROWS:
        label, _ = plan_item_label(adapter_key, native_query)
        assert label, adapter_key
        assert len(label) <= LABEL_MAX_CHARS, (adapter_key, label)


def test_an_unnameable_query_falls_back_to_the_key_the_fetch_ledger_uses() -> None:
    """The one row nobody can name is still findable, in the ledger's own words.

    ``#fa052a4f`` is not decoration: it is the first eight characters of the
    ``target_key`` this item's fetches are written under (N4), so a reader who
    cannot tell what the row is can still grep the ledger for it.
    """
    query = {"vendor": "ashby", "board_slugs": [], "keywords": [], "countries": ["BE"]}
    label, _ = plan_item_label("ats.unknownvendor", query)
    assert label == "#" + target_key("ats.unknownvendor", query)[:8]
    assert len(label) == 9


def test_an_empty_query_is_labelled_with_its_adapter_rather_than_with_nothing() -> None:
    """FR-162: there is no target to name, and a blank row is not the answer."""
    assert plan_item_label("board.generic", {})[0] == "board.generic"
    assert plan_item_label("board.generic", None)[0] == "board.generic"
    assert plan_item_label("board.generic", "not a dict at all")[0] == "board.generic"


# ---------------------------------------------------------------------------
# The scalar-versus-list principle (FR-162)
# ---------------------------------------------------------------------------


def test_a_scalar_search_separates_siblings_that_share_a_board() -> None:
    """Seven Workday rows, one Deloitte board, seven different questions.

    A *list* under ``keywords`` is the campaign's scoring vocabulary and is
    byte-identical on every item of the plan, so labelling from it names all
    seven the same.  A *scalar* ``search_text`` is the one query this item
    sends, and is the only thing that tells them apart.
    """
    base = {"slug": "deloitteie.wd3.myworkdayjobs.com/Experienced_Professionals",
            "company_name": "DELOITTE", "keywords": KEYWORD_BAG}
    first, _ = plan_item_label("ats.workday", {**base, "search_text": "Data Engineer"})
    second, _ = plan_item_label("ats.workday", {**base, "search_text": "Platform Architect"})
    assert first == "DELOITTE · Data Engineer"
    assert second == "DELOITTE · Platform Architect"
    assert first != second


def test_two_items_that_read_one_board_share_a_label_and_share_a_target() -> None:
    """The correct answer for a duplicate row is not a different name.

    The reuse assessment leaves a ``skipped`` twin beside a running item, and
    re-planning has occasionally produced two runnable rows for one board.
    Those are not two units of work with two names: they are the same fetch,
    planned twice.  The label says so by agreeing, and ``target_key`` is what
    the screen collapses them on (FR-166, FR-342).
    """
    one = {"slug": "credium", "company_name": "credium GmbH", "max_records": 500,
           "keywords": KEYWORD_BAG}
    two = {"slug": "credium", "company_name": "credium GmbH", "max_records": 250,
           "keywords": KEYWORD_BAG}
    assert plan_item_label("ats.personio", one) == plan_item_label("ats.personio", two)
    assert target_key("ats.personio", one) == target_key("ats.personio", two) == (
        "personio/credium"
    )


# ---------------------------------------------------------------------------
# Regressions
# ---------------------------------------------------------------------------


def test_the_pre_fix_actiris_shape_still_labels() -> None:
    """The twenty-row plan this product used to write is still readable.

    ``board.actiris`` planned one item per page, each carrying its own ``page``
    - which ``collection._run_page`` then overwrote with its own counter, so all
    twenty fetched page 1.  The adapter no longer writes ``page``, but 20 such
    rows are in the installed database and the screen still has to draw them.
    """
    label, detail = plan_item_label(
        "board.actiris",
        {"language": "nl", "page": 1, "offers_per_page": 50, "max_age_days": 30,
         "keywords": ["full-stack developer", "software engineer", "AI engineer",
                      "data engineer", "application developer"],
         "title_filter": False},
    )
    assert label == "full-stack developer +4 · page 1"
    assert detail == "nl"


def test_a_long_term_is_clipped_before_it_squeezes_out_the_company() -> None:
    """One 90-character Workday search must not be the whole label."""
    label, _ = plan_item_label(
        "ats.workday",
        {"slug": "acme.wd3.myworkdayjobs.com/External", "company_name": "ACME",
         "search_text": "Principal Engineer, Distributed Data Platforms and Streaming Systems"},
    )
    assert label.startswith("ACME · Principal Engineer, Dis")
    assert label.endswith("…")
    assert len(label) <= LABEL_MAX_CHARS


def test_a_hostname_is_named_by_its_site_and_not_by_its_door() -> None:
    """``jobs.northwind.com`` and ``northwind.com`` are one target to a reader."""
    assert plan_item_label("website.crawl", {"url": "https://jobs.northwind.com/x"})[0] == (
        "northwind.com"
    )
    assert plan_item_label("website.crawl", {"crawl_seeds": ["www.northwind.com"],
                                             "paths": ["/careers", "/jobs"]}) == (
        "northwind.com", "2 paths"
    )


def test_short_source_name_drops_the_parenthetical_a_log_line_has_no_room_for() -> None:
    """FR-361: 44 characters before the item's own label has even started."""
    assert short_source_name("EURES (European Job Mobility Portal)") == "EURES"
    assert short_source_name("Actiris (Brussels public employment service)") == "Actiris"
    assert short_source_name("Greenhouse") == "Greenhouse"
    assert short_source_name("") == ""
