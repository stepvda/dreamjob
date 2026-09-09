"""What a persisted plan item asks an adapter for, and what it must report back.

Two things were true of every collection run in the live database and neither
was visible anywhere: the plan items the campaign planner persisted asked for
something no adapter read, and an adapter that fetched nothing - or was refused
outright - reported the same "nothing found" as a board with no vacancies.

The tests here pin both halves:

* **FR-162** - a ``source_plan_item`` row is the contract.  The payloads below
  are copied verbatim out of the live ``source_plan_item`` table, and they are
  handed straight to ``fetch()`` rather than being re-written into the shape the
  adapter happens to prefer.  That is exactly what the existing adapter tests
  did not do, which is why they were green while production collected nothing.
* **FR-182, FR-185, NFR-403** - "robots.txt refused this", "every request
  failed", "the plan item names nothing to fetch" and "the page parsed to
  nothing" are four different answers, and the collection worker can only see
  them if the adapter raises or records them.

Nothing here touches the network: every response is a captured fixture or a
recorded payload.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from dreamjob.adapters import load_all
from dreamjob.adapters.ats.common import company_targets
from dreamjob.adapters.ats.detect import detect_ats
from dreamjob.adapters.ats.greenhouse import GreenhouseAdapter
from dreamjob.adapters.ats.personio import PersonioAdapter
from dreamjob.adapters.ats.recruitee import RecruiteeAdapter
from dreamjob.adapters.base import PlanItem, RawRecord, get_adapter
from dreamjob.adapters.jobboards.eures import DEFAULT_API_BASE, EuresAdapter
from dreamjob.adapters.jobboards.generic_html import is_extracted
from dreamjob.adapters.vacancy_source import (
    SourceUnavailable,
    UnusableQuery,
    fte_percentage,
    jsonld_jobpostings,
    normalise_contract_type,
)
from dreamjob.config import get_settings
from dreamjob.db.connection import query_one
from dreamjob.db.migrator import migrate
from dreamjob.egress.client import RateLimited, RobotsDisallowed

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def fixture(*parts: str) -> str:
    return FIXTURES.joinpath(*parts).read_text(encoding="utf-8")


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
# Stub egress
# ---------------------------------------------------------------------------


@dataclass
class StubResponse:
    url: str
    text: str
    status_code: int = 200
    raw_document_id: str | None = "raw-1"

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


@dataclass
class StubEgress:
    """Serves a recorded body for any matching URL, and records every call."""

    pages: dict[str, str] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    bodies: list[dict] = field(default_factory=list)
    default_status: int = 404
    raises: Exception | None = None

    async def fetch(self, url: str, **kwargs: Any) -> StubResponse:
        self.calls.append(url)
        self.bodies.append(dict(kwargs.get("json") or {}))
        if self.raises is not None:
            raise self.raises
        for pattern, body in self.pages.items():
            if pattern in url:
                return StubResponse(url=url, text=body)
        return StubResponse(url=url, text="", status_code=self.default_status)


# ---------------------------------------------------------------------------
# FR-162: the plan item the planner persisted is the one the adapter executes
# ---------------------------------------------------------------------------

#: Verbatim ``source_plan_item.native_query`` of every live board plan item.
PERSISTED_BOARD_QUERY = {
    "keywords": ["AI Engineer"],
    "location": "Belgium",
    "filters": {},
    "page": 1,
    "countries": ["BE"],
}

#: Verbatim ``source_plan_item.native_query`` of a live ATS plan item, once a
#: company with a board is in scope (the empty-list case is the planner's).
PERSISTED_ATS_QUERY = {
    "vendor": "greenhouse",
    "board_slugs": ["gitlab"],
    "keywords": ["AI Engineer"],
    "countries": ["BE"],
}


@pytest.mark.parametrize(
    ("key", "expected_url"),
    [
        (
            "board.jobat",
            "https://www.jobat.be/nl/jobs?trefwoord=AI+Engineer&plaats=Belgium&pagina=1",
        ),
        (
            "board.stepstone",
            "https://www.stepstone.be/jobs/AI+Engineer?q=AI+Engineer&where=Belgium&page=1",
        ),
    ],
)
async def test_a_persisted_board_plan_item_carries_its_keyword_into_the_search(
    db, key, expected_url
):
    """The planner writes ``keywords``/``location``; the boards read them.

    Every board fetch in the live database went out as ``?trefwoord=&plaats=``
    because the adapter read ``queries``/``locations`` and nothing translated.
    One character of difference, and every search ran empty.
    """
    load_all()
    egress = StubEgress()
    adapter = get_adapter(key, egress)
    with pytest.raises(SourceUnavailable):        # the stub answers 404
        await adapter.fetch(PlanItem(key, dict(PERSISTED_BOARD_QUERY)))
    assert egress.calls == [expected_url]


async def test_a_persisted_eures_plan_item_searches_for_its_keyword_and_country(db):
    """FR-162: the EURES search body must carry the planner's keyword and country."""
    load_all()
    egress = StubEgress({"jv-search/search": fixture("boards", "eures_search_be.json")})
    adapter = get_adapter("board.eures", egress)
    records = await adapter.fetch(PlanItem("board.eures", dict(PERSISTED_BOARD_QUERY)))

    assert egress.calls[0].startswith(DEFAULT_API_BASE)
    body = egress.bodies[0]
    assert body["keywords"] == [{"keyword": "AI Engineer", "specificSearchCode": "EVERYWHERE"}]
    assert body["locationCodes"] == ["BE"]
    assert records and records[0].meta["kind"] == "list"
    # FR-186: a detail per summary is fifty rate-limited requests for an
    # employer name, so details are asked for explicitly or not at all.
    assert len(egress.calls) == 1


def test_eures_reads_the_shapes_the_live_service_answers_with():
    """The search summary and the detail document, captured from the live API.

    Two things were wrong at once and each hid the other: the summary's
    ``locationMap`` is a country-to-NUTS-region mapping, not an address, so
    reading ``cityName`` off it left every vacancy with a NULL location and a
    NULL country; and the detail document nests everything under
    ``jvProfiles.<lang>``, one level below where the parser looked.
    """
    adapter = EuresAdapter()
    search = fixture("boards", "eures_search_be.json")
    summaries = adapter.parse(
        RawRecord(url="https://europa.eu/x", content=search, content_type="application/json",
                  meta={"kind": "list", "lang": "en", "emit": True})
    )
    assert [r["title"] for r in summaries] == ["Automation Engineer", "Product Engineer R&D"]
    assert {r["country"] for r in summaries} == {"BE"}          # from locationMap's key
    assert summaries[0]["company_name_raw"] is None             # the summary states none
    assert summaries[1]["company_name_raw"] == "2beGood"
    assert summaries[0]["posted_at"].startswith("2026-")        # creationDate is epoch ms

    detail = adapter.parse(
        RawRecord(
            url="https://europa.eu/eures/portal/jv-se/jv-details/x?lang=en",
            content=fixture("boards", "eures_detail_fr.json"),
            content_type="application/json",
            meta={"kind": "detail", "lang": "en",
                  "summary": json.loads(search)["jvs"][0]},
        )
    )[0]
    assert detail["title"] == "Automation Engineer"
    assert detail["location"] == "Fleurus, BE"                  # jvProfiles.<lang>.locations[0]
    assert detail["country"] == "BE"
    assert detail["application_target"] == "https://easyapply.jobs/r/DFyQJcD27ahTUD2yjaxf"


def test_the_eures_endpoints_are_the_ones_the_portal_serves_today():
    """The withdrawn search path answered 404 for every campaign in the database."""
    assert DEFAULT_API_BASE == "https://europa.eu/eures/api/jv-searchengine/public"
    assert "eures-apps/searchengine" not in DEFAULT_API_BASE
    assert EuresAdapter.detail_url(DEFAULT_API_BASE, "MTIwNDMwMzQgNDQx", "en") == (
        "https://europa.eu/eures/api/jv-searchengine/public/jv/id/MTIwNDMwMzQgNDQx"
        "?requestLang=en&preferredLang=en"
    )
    body = EuresAdapter.search_body("engineer", ["BE"], 1, 50)
    assert body["keywords"] == [{"keyword": "engineer", "specificSearchCode": "EVERYWHERE"}]
    assert "keywordType" not in json.dumps(body)   # the retired shape is a 400


async def test_a_persisted_ats_plan_item_reads_the_board_it_names(db):
    """The planner's ``board_slugs`` shape must work end to end, not only ``slug``."""
    load_all()
    egress = StubEgress({"boards-api.greenhouse.io": fixture("ats", "greenhouse_gitlab_jobs.json")})
    adapter = get_adapter("ats.greenhouse", egress)
    row = {"adapter_key": "ats.greenhouse", "native_query": dict(PERSISTED_ATS_QUERY)}
    records = await adapter.run(row)
    assert egress.calls == [
        "https://boards-api.greenhouse.io/v1/boards/gitlab/jobs?content=true"
    ]
    assert records and all(r.entity_type == "vacancy" for r in records)


def test_the_ats_title_filter_is_off_unless_the_plan_item_asks_for_it(db):
    """FR-162: a board is the employer's own list; filtering it is a choice."""
    load_all()
    adapter = GreenhouseAdapter()
    body = fixture("ats", "greenhouse_gitlab_jobs.json")
    meta = adapter.board_meta(
        PlanItem("ats.greenhouse", {"slug": "gitlab", "keywords": ["AI Engineer"]}),
        "gitlab",
    )
    unfiltered = adapter.parse(
        RawRecord(url="https://x", content=body, content_type="application/json", meta=meta)
    )
    filtered = adapter.parse(
        RawRecord(
            url="https://x", content=body, content_type="application/json",
            meta={**meta, "title_filter": True},
        )
    )
    assert [r["title"] for r in unfiltered] == ["Account Executive - Italy", "AI Engineer"]
    assert [r["title"] for r in filtered] == ["AI Engineer"]


# ---------------------------------------------------------------------------
# FR-182 / FR-185: a source that could not be read fails its own plan item
# ---------------------------------------------------------------------------


async def test_a_board_slug_that_does_not_exist_fails_instead_of_reporting_nothing(db):
    """A 404 board and an empty board looked identical: ``done``, 0 records, 0 errors."""
    load_all()
    egress = StubEgress(default_status=404)
    adapter = get_adapter("ats.greenhouse", egress)
    with pytest.raises(SourceUnavailable) as raised:
        await adapter.run(PlanItem("ats.greenhouse", {"slug": "no-such-board-zzz"}))
    assert "404" in str(raised.value)


async def test_a_rate_limited_board_fails_instead_of_reporting_nothing(db):
    """A bot checkpoint answering 429 is a refusal, not an empty board."""
    load_all()
    egress = StubEgress(raises=RateLimited("api.example returned 429 repeatedly"))
    adapter = get_adapter("ats.lever", egress)
    with pytest.raises(SourceUnavailable):
        await adapter.run(PlanItem("ats.lever", {"slug": "acme"}))


async def test_an_unconfigured_generic_board_says_so_instead_of_collecting_nothing(db):
    """``board.generic`` inherits no route at all, so it issues no request.

    Eight of its plan items sat in the live database at ``done`` with zero
    records, and not one welcometothejungle- or generic-board URL was ever
    fetched.
    """
    load_all()
    egress = StubEgress()
    adapter = get_adapter("board.generic", egress)
    with pytest.raises(UnusableQuery) as raised:
        await adapter.run(PlanItem("board.generic", dict(PERSISTED_BOARD_QUERY)))
    assert egress.calls == []
    assert "url_template" in str(raised.value)


def test_a_board_with_no_route_is_catalogued_as_disabled(db):
    """FR-161/FR-164: a source that cannot reach anything is not selectable."""
    load_all()
    for key in ("board.generic", "board.wttj", "board.vdab"):
        row = query_one(
            "SELECT enabled FROM source_catalogue WHERE adapter_key = ?", (key,)
        )
        assert row["enabled"] == 0, f"{key} is catalogued as enabled but has no route"
        assert get_adapter(key).is_enabled() is False
    # A board that does have a route stays selectable.
    assert query_one(
        "SELECT enabled FROM source_catalogue WHERE adapter_key = ?", ("board.jobat",)
    )["enabled"] == 1


def test_an_administrator_who_enables_a_configured_board_keeps_it_enabled(db):
    """IR-101/FR-363: the catalogue is the administrator's, not the adapter's.

    Registering must not undo a decision that was made deliberately, or the
    board an operator configured would switch itself off at the next restart.
    """
    from dreamjob.db.connection import execute
    from dreamjob.db.repositories import admin as admin_repo

    load_all()
    admin_repo.update_source("board.generic", {"acknowledged_at": "2026-09-09T00:00:00+00:00",
                                               "enabled": 1})
    load_all()                                   # a restart re-registers everything
    assert query_one(
        "SELECT enabled FROM source_catalogue WHERE adapter_key = ?", ("board.generic",)
    )["enabled"] == 1
    execute("UPDATE source_catalogue SET acknowledged_at = NULL WHERE adapter_key = ?",
            ("board.generic",))


async def test_welcome_to_the_jungle_without_credentials_names_what_is_missing(db):
    """Its default mode has no route, and its own docstring claimed otherwise."""
    load_all()
    egress = StubEgress()
    adapter = get_adapter("board.wttj", egress)
    with pytest.raises(UnusableQuery):
        await adapter.run(PlanItem("board.wttj", {"mode": "algolia", "queries": ["data"]}))
    assert egress.calls == []


async def test_a_page_that_parses_to_nothing_collapses_the_extraction_rate(db):
    """NFR-403 could never fire: with zero raw records the rate stayed None.

    A listing that answers 200 and yields no card is the signal that the board
    changed its markup (or renders client-side).  It has to be counted as an
    attempt that failed, not as an absence of attempts.
    """
    load_all()
    egress = StubEgress({"jobat.be": "<html><body><p>Geen resultaten</p></body></html>"})
    adapter = get_adapter("board.jobat", egress)
    records = await adapter.run(PlanItem("board.jobat", dict(PERSISTED_BOARD_QUERY)))
    assert records == []
    assert egress.calls, "the board was fetched"
    assert adapter.extraction_rate == 0.0
    assert adapter.report_extraction_rate() == 0.0
    assert query_one(
        "SELECT extraction_success_rate FROM source_catalogue WHERE adapter_key = ?",
        ("board.jobat",),
    )["extraction_success_rate"] == 0.0


async def test_robots_txt_reaches_the_worker_rather_than_a_log_line(db):
    """FR-182: SmartRecruiters is Disallow:/ for everyone but LinkedInBot."""
    load_all()
    egress = StubEgress(raises=RobotsDisallowed("robots.txt disallows api.smartrecruiters.com"))
    adapter = get_adapter("ats.smartrecruiters", egress)
    with pytest.raises(RobotsDisallowed):
        await adapter.run(PlanItem("ats.smartrecruiters", {"slug": "Ubisoft"}))


async def test_personio_stops_the_detail_walk_at_the_first_bot_checkpoint(db):
    """*.jobs.personio.de answers 429 to this user agent (Vercel checkpoint).

    Sixty positions x three attempts x an exponential back-off bought nothing;
    the feed records are kept, the walk stops, and the block is counted as a
    failed extraction so NFR-403 can see it.
    """
    load_all()
    egress = StubEgress({"/xml": fixture("ats", "personio_personio_feed.xml")},
                        default_status=429)
    adapter = get_adapter("ats.personio", egress)
    records = await adapter.run(
        PlanItem("ats.personio", {"slug": "personio", "fetch_details": True})
    )
    detail_calls = [c for c in egress.calls if "/job/" in c]
    assert len(detail_calls) == 1, "the walk must stop at the first refusal"
    assert records, "the feed records survive without their advert text"
    assert adapter.extraction_rate is not None and adapter.extraction_rate < 1.0


# ---------------------------------------------------------------------------
# FR-183: deterministic extraction
# ---------------------------------------------------------------------------


def test_a_listing_marked_up_as_an_itemlist_is_extracted():
    """Google's documented shape for a page listing several postings.

    The walker recursed into ``itemListElement`` but not into the ``item`` key
    below it, so it yielded the ``ListItem`` wrappers, ``_is_jobposting``
    rejected all of them, and the cheapest path in the design - one listing
    request instead of N detail requests - never fired.
    """
    page = """
    <html><head><script type="application/ld+json">
    {"@context":"https://schema.org","@type":"ItemList","itemListElement":[
      {"@type":"ListItem","position":1,"item":{"@type":"JobPosting",
        "title":"Data Engineer","hiringOrganization":{"name":"Acme"},
        "description":"<p>Python en SQL.</p>"}},
      {"@type":"ListItem","position":2,"item":{"@type":"JobPosting",
        "title":"Data Analist","hiringOrganization":{"name":"Acme"},
        "description":"<p>Power BI.</p>"}}]}
    </script></head><body></body></html>
    """
    assert [p["title"] for p in jsonld_jobpostings(page)] == ["Data Engineer", "Data Analist"]


def test_a_cdata_wrapped_jsonld_block_is_read():
    """Drupal and several Java CMSs wrap ld+json in CDATA; json.loads chokes on it."""
    page = (
        '<html><head><script type="application/ld+json">//<![CDATA[\n'
        '{"@type":"JobPosting","title":"CDATA job"}\n//]]></script></head></html>'
    )
    assert [p["title"] for p in jsonld_jobpostings(page)] == ["CDATA job"]


def test_a_client_rendered_shell_is_not_an_advert():
    """VDAB's detail pages are ~46 KB of "Toepassing laden..." and nothing else."""
    assert is_extracted("Toepassing laden...") is False
    assert is_extracted("Nieuw!") is False
    assert is_extracted("Je bouwt datapijplijnen voor onze klanten. " * 5) is True


def test_vdab_no_longer_takes_the_first_heading_for_the_employer():
    """``h2`` and ``main`` matched the badge and the loading shell of the SPA.

    Nothing is written today because the title selector matches nothing either;
    the moment it did, the row would have said the employer was "Nieuw!" and
    the advert was "Toepassing laden...", at confidence 0.7.
    """
    from dreamjob.adapters.jobboards.vdab import VdabAdapter

    detail = VdabAdapter.defaults["detail_selectors"]
    assert "h2" not in detail["company"]
    assert "main" not in detail["description"]
    assert VdabAdapter().has_route() is False        # no advert URLs -> no route


# ---------------------------------------------------------------------------
# FR-261: what the advert states is what is stored
# ---------------------------------------------------------------------------


def test_a_statistic_in_the_advert_is_not_a_working_time():
    """Every one of GitLab's 227 live postings was stored as 50% FTE.

    The boilerplate says "more than 50% of the Fortune 100", and the extractor
    took the first percentage anywhere in the first 1500 characters.
    """
    adapter = GreenhouseAdapter()
    meta = adapter.board_meta(PlanItem("ats.greenhouse", {"slug": "gitlab"}), "gitlab")
    rows = adapter.parse(
        RawRecord(
            url="https://boards-api.greenhouse.io/v1/boards/gitlab/jobs?content=true",
            content=fixture("ats", "greenhouse_gitlab_jobs.json"),
            content_type="application/json",
            meta=meta,
        )
    )
    assert rows
    assert {r["fte_percentage"] for r in rows} == {None}

    # A working time that is actually stated is still read.
    assert fte_percentage("Stellenumfang: 80%") == 80
    assert fte_percentage("This is an 80% FTE position") == 80
    assert fte_percentage("Part-time") == 50
    assert fte_percentage("Full-time") == 100
    assert fte_percentage("more than 50% of the Fortune 100 trust us") is None


def test_a_monthly_salary_is_not_stored_as_an_annual_one():
    """Recruitee states a period; the vacancy columns are annual (FR-261)."""
    adapter = RecruiteeAdapter()
    meta = adapter.board_meta(PlanItem("ats.recruitee", {"slug": "channable"}), "channable")
    rows = adapter.parse(
        RawRecord(
            url="https://channable.recruitee.com/api/offers/",
            content=fixture("ats", "recruitee_channable_offers.json"),
            content_type="application/json",
            meta=meta,
        )
    )
    stated = json.loads(fixture("ats", "recruitee_channable_offers.json"))["offers"]
    assert [o["salary"]["period"] for o in stated] == ["month", "month"]
    by_title = {r["title"]: r for r in rows}
    support = by_title["Technischer Kundensupport DACH - Deutschsprachig"]
    assert support["salary_min"] == 2850 * 12
    assert support["salary_max"] == 2950 * 12
    assert support["salary_currency"] == "EUR"

    # A period that cannot be annualised without inventing a working week is
    # not stored at all: a wrong number is worse than no number.
    hourly = adapter._one(
        {"title": "x", "salary": {"min": "25", "max": "30", "period": "hour",
                                  "currency": "EUR"}},
        RawRecord(url="https://x", content="{}", meta=meta),
    )
    assert (hourly["salary_min"], hourly["salary_max"]) == (None, None)


def test_a_compound_employment_code_still_states_a_contract_type():
    """13 of 18 live Recruitee offers lost the contract type they stated.

    ``fulltime_permanent`` never matched, because ``_`` is a word character and
    the patterns are anchored on word boundaries.
    """
    assert normalise_contract_type("fulltime_permanent") == "permanent"
    assert normalise_contract_type("parttime_permanent") == "permanent"
    assert normalise_contract_type("fulltime_temporary") == "interim"
    assert normalise_contract_type("parttime_internship") == "fixed_term"
    assert normalise_contract_type("permanent") == "permanent"
    # The false positive the word boundaries were there to stop stays stopped.
    assert normalise_contract_type("We are an international company") is None


def test_recruitee_reads_the_contract_type_off_a_real_offer():
    adapter = RecruiteeAdapter()
    meta = adapter.board_meta(PlanItem("ats.recruitee", {"slug": "nmbrs"}), "nmbrs")
    rows = adapter.parse(
        RawRecord(
            url="https://nmbrs.recruitee.com/api/offers/",
            content=fixture("ats", "recruitee_nmbrs_offers.json"),
            content_type="application/json",
            meta=meta,
        )
    )
    assert rows and {r["contract_type"] for r in rows} == {"fixed_term"}   # fulltime_fixed_term


# ---------------------------------------------------------------------------
# FR-181/FR-222: which board a company runs, and whose plan item it becomes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("html", "url", "expected"),
    [
        # The EU pod: the host has a regional segment the pattern could not match.
        ("", "https://job-boards.eu.greenhouse.io/acme/jobs/1", ("greenhouse", "acme")),
        # An asset host is not a tenant called "cdn".
        (
            '<script src="https://cdn.teamtailor.com/x.js"></script>'
            '<a href="https://acme.teamtailor.com/jobs">Jobs</a>',
            "https://acme.com/careers",
            ("teamtailor", "acme"),
        ),
        # A board on the company's own domain names no tenant anywhere, but the
        # vendor is still worth recording - the docstring always promised it.
        (
            '<img src="https://assets-aws.teamtailor-cdn.com/logo.png">',
            "https://jobs.acme.eu/jobs",
            ("teamtailor", None),
        ),
        ("<p>We hire by email.</p>", "https://acme.com/careers", (None, None)),
    ],
)
def test_detect_ats_edges(html, url, expected):
    assert detect_ats(html, url) == expected


def test_a_company_row_without_a_vendor_is_not_planned_for_every_vendor():
    """A slug is only meaningful under the vendor whose board it names.

    Fanning one slug out to all seven adapters means six silent 404s - which,
    before the fix above, were six plan items reported as ``done``.
    """
    directives = {
        "companies": [
            {"name": "Acme", "ats_slug": "acme"},                          # no vendor
            {"name": "Beta", "ats_vendor": "greenhouse", "ats_slug": "beta"},
        ]
    }
    assert [t["slug"] for t in company_targets(directives, {}, "greenhouse")] == ["beta"]
    assert company_targets(directives, {}, "lever") == []


async def test_personio_feed_positions_survive_a_missing_detail_page(db):
    """The feed is the record of last resort; a blocked detail must not lose it."""
    load_all()
    egress = StubEgress({"/xml": fixture("ats", "personio_personio_feed.xml")},
                        default_status=403)
    adapter = PersonioAdapter(egress)
    records = await adapter.run(PlanItem("ats.personio", {"slug": "personio"}))
    assert records and records[0].entity_type == "vacancy"
