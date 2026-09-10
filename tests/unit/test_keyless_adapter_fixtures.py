"""The four new keyless adapters, against *captured real* responses (plan N8).

Every fixture used here is a verbatim (only truncated) response captured from
the live endpoint on 2026-09-09 with the product's own User-Agent and no
credentials:

    Workable     POST apply.workable.com/api/v3/accounts/{3e,european-dynamics}/jobs
                 GET  apply.workable.com/api/v1/widget/accounts/3e?details=true
    Teamtailor   GET  bearingpointnetherlands.teamtailor.com/jobs.rss
    Actiris      GET  www.actiris.brussels/sitemapoffers-nl.xml
                 GET  www.actiris.brussels/{nl,fr}/.../?reference=...
    Arbeitnow    GET  www.arbeitnow.com/api/job-board-api?page=1

Nothing here touches the network.  The point of parsing the real shapes offline
is that a vendor renaming a field fails *here*, loudly, instead of turning a
board into a silent zero - which is the failure mode the data-gathering plan
was written to end (docs/Data_Gathering_Plan.md 3, step 8).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from dreamjob.adapters import load_all
from dreamjob.adapters.ats.teamtailor import TeamtailorAdapter
from dreamjob.adapters.ats.workable import WorkableAdapter
from dreamjob.adapters.base import PlanItem, RawRecord, all_adapters, get_adapter
from dreamjob.adapters.jobboards.actiris import OFFERS_PER_PAGE, ActirisAdapter
from dreamjob.adapters.jobboards.arbeitnow import ArbeitnowAdapter
from dreamjob.adapters.vacancy_source import VACANCY_COLUMNS, SourceUnavailable
from dreamjob.config import get_settings
from dreamjob.db.connection import query_one
from dreamjob.db.migrator import migrate
from dreamjob.egress.client import RobotsDisallowed

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
    """Serves a captured body per URL fragment and records every call.

    ``bodies`` keeps the JSON payload of each request, which is how the
    Workable pagination test can see whether the cursor was sent back.
    """

    pages: dict[str, str] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    bodies: list[dict] = field(default_factory=list)
    default_status: int = 404
    raises: Exception | None = None
    #: ``(url fragment, request-body key) -> body``, tried before ``pages``.
    keyed: dict[tuple[str, str], str] = field(default_factory=dict)

    async def fetch(self, url: str, **kwargs: Any) -> StubResponse:
        self.calls.append(url)
        payload = dict(kwargs.get("json") or {})
        self.bodies.append(payload)
        if self.raises is not None:
            raise self.raises
        for (fragment, token), body in self.keyed.items():
            if fragment in url and payload.get("token") == token:
                return StubResponse(url=url, text=body)
        for fragment, body in self.pages.items():
            if fragment in url:
                return StubResponse(url=url, text=body)
        return StubResponse(url=url, text="", status_code=self.default_status)


# ---------------------------------------------------------------------------
# The captured shapes parse
# ---------------------------------------------------------------------------

WORKABLE_META = {
    "slug": "3e",
    "company_id": None,
    "company_name": None,
    "max_records": 500,
    "keywords": [],
    "title_filter": False,
    "kind": "list",
    "page": 1,
}


def workable_details() -> tuple[str, dict[str, dict]]:
    widget = json.loads(fixture("ats", "workable_3e_widget.json"))
    details: dict[str, dict] = {}
    for job in widget["jobs"]:
        details.setdefault(job["shortcode"], job)
    return widget["name"], details


def workable_record() -> RawRecord:
    account, details = workable_details()
    return RawRecord(
        url="https://apply.workable.com/api/v3/accounts/3e/jobs#page=1",
        content=fixture("ats", "workable_3e_jobs.json"),
        content_type="application/json",
        raw_document_id="raw-1",
        meta={**WORKABLE_META, "details": details, "account_name": account},
    )


def teamtailor_record() -> RawRecord:
    return RawRecord(
        url="https://bearingpointnetherlands.teamtailor.com/jobs.rss",
        content=fixture("ats", "teamtailor_bearingpointnetherlands_jobs.rss"),
        content_type="application/rss+xml",
        raw_document_id="raw-1",
        meta={
            "slug": "bearingpointnetherlands",
            "company_id": None,
            "company_name": None,
            "max_records": 500,
            "keywords": [],
            "title_filter": False,
        },
    )


def actiris_record(language: str = "nl") -> RawRecord:
    urls = {
        "nl": "https://www.actiris.brussels/nl/burgers/jobadvertentie/?reference=5948551",
        "fr": "https://www.actiris.brussels/fr/citoyens/detail-offre-d-emploi/?reference=5948550",
    }
    return RawRecord(
        url=urls[language],
        content=fixture("boards", f"actiris_offer_{language}.html"),
        content_type="text/html",
        raw_document_id="raw-1",
        meta={
            "language": language,
            "reference": urls[language].rsplit("=", 1)[-1],
            "lastmod": "2026-09-09",
            "keywords": [],
            "title_filter": False,
        },
    )


def arbeitnow_record() -> RawRecord:
    return RawRecord(
        url="https://www.arbeitnow.com/api/job-board-api?page=1",
        content=fixture("boards", "arbeitnow_job_board_page1.json"),
        content_type="application/json",
        raw_document_id="raw-1",
        meta={"page": 1, "keywords": [], "title_filter": False, "max_records": 250},
    )


CASES = [
    pytest.param(WorkableAdapter(), workable_record(), id="workable"),
    pytest.param(TeamtailorAdapter(), teamtailor_record(), id="teamtailor"),
    pytest.param(ActirisAdapter(), actiris_record("nl"), id="actiris-nl"),
    pytest.param(ActirisAdapter(), actiris_record("fr"), id="actiris-fr"),
    pytest.param(ArbeitnowAdapter(), arbeitnow_record(), id="arbeitnow"),
]


@pytest.mark.parametrize(("adapter", "record"), CASES)
def test_parse_reads_the_live_response_shape(adapter, record):
    parsed = adapter.parse(record)
    assert parsed, f"{adapter.key} parsed nothing from a real response"
    for row in parsed:
        assert (row.get("title") or "").strip(), f"{adapter.key} produced a posting without a title"
        assert row.get("source_url"), f"{adapter.key} produced a posting without a source URL"


@pytest.mark.parametrize(("adapter", "record"), CASES)
def test_normalise_matches_the_vacancy_columns(adapter, record):
    """FR-261: every normalised key is a real ``vacancy`` column."""
    for parsed in adapter.parse(record):
        normalised = adapter.normalise(parsed, record)
        assert normalised is not None, f"{adapter.key} dropped a posting during normalise"
        assert normalised.entity_type == "vacancy"
        unknown = set(normalised.data) - VACANCY_COLUMNS
        assert not unknown, f"{adapter.key} wrote unknown vacancy columns: {sorted(unknown)}"
        assert normalised.data["source_adapter"] == adapter.key
        assert normalised.data["dedup_key"]
        assert normalised.data["raw_document_id"] == "raw-1"


@pytest.mark.parametrize(
    "adapter",
    [WorkableAdapter(), TeamtailorAdapter(), ActirisAdapter(), ArbeitnowAdapter()],
    ids=lambda a: a.key,
)
def test_these_sources_never_call_the_llm(adapter):
    """Plan 6.9: all four parse deterministically and must cost 0 tokens."""
    assert adapter.llm_fallback is False
    assert adapter.llm_extract("some advert text", "https://example.invalid") is None


def test_the_four_new_adapters_are_discovered_and_catalogued(db):
    """NFR-601/FR-161: dropping the module in is the whole registration."""
    load_all()
    keys = {"ats.workable", "ats.teamtailor", "board.actiris", "board.arbeitnow"}
    assert keys <= set(all_adapters())
    for key in keys:
        row = query_one(
            "SELECT enabled, tos_status, legal_notes FROM source_catalogue WHERE adapter_key = ?",
            (key,),
        )
        assert row is not None, f"{key} was not written to the catalogue"
        assert row["enabled"] == 1
        assert row["tos_status"] == "permitted"
        assert row["legal_notes"], f"{key} states no legal position"
        assert get_adapter(key).is_enabled() is True


# ---------------------------------------------------------------------------
# Workable
# ---------------------------------------------------------------------------


def test_workable_reads_the_fields_the_live_board_actually_sends():
    row = WorkableAdapter().parse(workable_record())[0]
    assert row["title"] == "SaaS Support Engineer Internship"
    assert row["company_name_raw"] == "3E"          # widget `name`, not in the list
    assert row["location"] == "Brussels, Brussels, Belgium"
    assert row["country"] == "BE"
    assert row["function_family"] == "SynaptiQ Operations"     # results[].department
    assert row["work_arrangement"] == "hybrid"                 # results[].workplace
    assert row["posted_at"].startswith("2026-08-24")           # results[].published
    assert row["source_url"] == "https://apply.workable.com/3e/j/A54691B31D/"
    # The advert text only exists in the widget response.
    assert len(row["description"]) > 500
    assert "<p>" not in row["description"]


def test_workable_does_not_publish_drafts_or_internal_postings():
    """``state`` and ``isInternal`` are the board's own visibility flags."""
    payload = json.loads(fixture("ats", "workable_3e_jobs.json"))
    payload["results"][0]["state"] = "draft"
    payload["results"][1]["isInternal"] = True
    record = workable_record()
    record.content = json.dumps(payload)
    titles = [r["title"] for r in WorkableAdapter().parse(record)]
    assert len(titles) == len(payload["results"]) - 2


async def test_workable_pages_by_posting_the_cursor_back_as_token(db):
    """The cursor is a *body* field named ``token``, not ``nextPage``.

    ``{"nextPage": ...}`` is answered ``400 {"nextPage": "Not allowed"}`` and
    the same value as a query parameter is ignored - which re-serves page one
    for ever, so a 156-posting board would have yielded its first ten rows and
    reported success.
    """
    load_all()
    page1 = json.loads(fixture("ats", "workable_european_dynamics_page1.json"))
    egress = StubEgress(
        keyed={
            ("/accounts/european-dynamics/jobs", None): json.dumps(page1),
            ("/accounts/european-dynamics/jobs", page1["nextPage"]): fixture(
                "ats", "workable_european_dynamics_page2.json"
            ),
        },
        pages={"/widget/accounts/": json.dumps({"name": "EUROPEAN DYNAMICS", "jobs": []})},
    )
    adapter = WorkableAdapter(egress)
    records = await adapter.fetch(
        PlanItem("ats.workable", {"slug": "european-dynamics", "max_records": 20})
    )
    assert len(records) == 2, "the second page was never requested"
    assert egress.bodies[0] == {}, "the first request must not carry a cursor"
    assert egress.bodies[1] == {"token": page1["nextPage"]}
    shortcodes = {
        row["source_url"] for record in records for row in adapter.parse(record)
    }
    assert len(shortcodes) == 6, "page two returned the same postings as page one"


async def test_workable_spends_one_max_records_budget_over_the_whole_board(db):
    """``max_records`` bounds the board, not each page of it.

    ``parse()`` applies the cap to one response at a time, so handing every
    page the same budget would let a three-page board emit three times the cap.
    """
    load_all()
    page1 = json.loads(fixture("ats", "workable_european_dynamics_page1.json"))
    egress = StubEgress(
        keyed={
            ("/accounts/european-dynamics/jobs", None): json.dumps(page1),
            ("/accounts/european-dynamics/jobs", page1["nextPage"]): fixture(
                "ats", "workable_european_dynamics_page2.json"
            ),
        },
        pages={"/widget/accounts/": json.dumps({"name": "EUROPEAN DYNAMICS", "jobs": []})},
    )
    adapter = WorkableAdapter(egress)
    records = await adapter.fetch(
        PlanItem("ats.workable", {"slug": "european-dynamics", "max_records": 20})
    )
    on_page_one = len(page1["results"])
    assert records[0].meta["max_records"] == 20
    assert records[1].meta["max_records"] == 20 - on_page_one, (
        "page two was handed the whole budget again"
    )
    rows = [row for record in records for row in adapter.parse(record)]
    assert len(rows) == 2 * on_page_one


async def test_workable_stops_on_total_rather_than_on_the_always_present_cursor(db):
    """A ten-posting board answers ``total: 10`` *and* a ``nextPage`` token.

    Following the token would spend one wasted request per board - 2 s each at
    the per-domain limit, once for every one of the ~2,500 live boards.
    """
    load_all()
    egress = StubEgress(
        pages={
            "/api/v3/accounts/3e/jobs": fixture("ats", "workable_3e_jobs.json"),
            "/widget/accounts/3e": fixture("ats", "workable_3e_widget.json"),
        }
    )
    adapter = WorkableAdapter(egress)
    records = await adapter.fetch(PlanItem("ats.workable", {"slug": "3e"}))
    assert len(records) == 1
    list_calls = [c for c in egress.calls if "/api/v3/" in c]
    widget_calls = [c for c in egress.calls if "/widget/" in c]
    assert len(list_calls) == 1, f"one board, one list request; got {list_calls}"
    assert len(widget_calls) == 1, "the advert text is one request per board"


async def test_workable_without_details_issues_no_second_request(db):
    """``fetch_details: false`` is the plan's cheaper shape and must stay cheap."""
    load_all()
    egress = StubEgress(pages={"/api/v3/accounts/3e/jobs": fixture("ats", "workable_3e_jobs.json")})
    adapter = WorkableAdapter(egress)
    records = await adapter.fetch(
        PlanItem("ats.workable", {"slug": "3e", "fetch_details": False})
    )
    assert [c for c in egress.calls if "/widget/" in c] == []
    rows = adapter.parse(records[0])
    assert rows and all(row["description"] == "" for row in rows)
    assert rows[0]["title"], "the structured fields survive without the advert text"


def test_workable_widget_duplicates_do_not_move_a_description_onto_another_posting():
    """A posting open in three cities is listed three times in the widget.

    Zipping the two responses positionally would have handed each posting the
    next one's advert text.
    """
    account, details = workable_details()
    widget = json.loads(fixture("ats", "workable_3e_widget.json"))
    assert len(widget["jobs"]) > len(details), "the fixture really does repeat a shortcode"
    rows = {r["source_url"]: r for r in WorkableAdapter().parse(workable_record())}
    multi = rows["https://apply.workable.com/3e/j/7CFC8D8302/"]
    assert "BESS" in multi["description"] or "FV" in multi["description"]
    assert multi["title"] == "Consultor de sistemas BESS y FV"


async def test_a_workable_board_that_answers_html_fails_its_plan_item(db):
    """FR-185: a changed endpoint is a failure, not an empty board."""
    load_all()
    egress = StubEgress(pages={"/api/v3/": "<html><body>Not Found</body></html>"})
    adapter = WorkableAdapter(egress)
    with pytest.raises(SourceUnavailable):
        await adapter.fetch(PlanItem("ats.workable", {"slug": "3e"}))


# ---------------------------------------------------------------------------
# Teamtailor
# ---------------------------------------------------------------------------


def test_teamtailor_reads_the_fields_the_live_feed_actually_sends():
    rows = TeamtailorAdapter().parse(teamtailor_record())
    row = rows[0]
    assert row["company_name_raw"] == "BearingPoint Netherlands"   # <channel><title>
    assert row["location"] == "Amsterdam, Netherlands"             # tt:locations
    assert row["country"] == "NL"
    assert row["function_family"] == "People & Strategy"           # tt:department
    assert row["work_arrangement"] == "hybrid"                     # remoteStatus
    assert row["contract_type"] == "fixed_term"                    # tt:role
    assert row["source_url"].startswith("https://bearingpointnetherlands.teamtailor.com/jobs/")
    assert len(row["description"]) > 500


def test_teamtailor_rfc822_dates_become_real_timestamps():
    """``parse_datetime`` reads ISO-8601 and epochs; a feed date is RFC 822.

    Unconverted, every Teamtailor posting was stored with ``posted_at`` NULL,
    which silently disables the 7-day vacancy staleness window.
    """
    assert TeamtailorAdapter._published("Wed, 24 Dec 2025 12:13:31 +0100") == (
        "2025-12-24T11:13:31+00:00"
    )
    assert TeamtailorAdapter._published("") is None
    assert TeamtailorAdapter._published("not a date") is None
    for row in TeamtailorAdapter().parse(teamtailor_record()):
        assert row["posted_at"], "a feed item lost its publication date"


async def test_a_teamtailor_tenant_that_answers_html_fails_its_plan_item(db):
    """A moved tenant answers 200 with a marketing page, not an RSS feed."""
    load_all()
    egress = StubEgress(pages={"teamtailor.com": "<!doctype html><html><body>Gone</body></html>"})
    adapter = get_adapter("ats.teamtailor", egress)
    with pytest.raises(SourceUnavailable):
        await adapter.run(PlanItem("ats.teamtailor", {"slug": "moved"}))


async def test_a_teamtailor_plan_item_reads_every_slug_it_names(db):
    load_all()
    egress = StubEgress(
        pages={"teamtailor.com": fixture("ats", "teamtailor_bearingpointnetherlands_jobs.rss")}
    )
    adapter = get_adapter("ats.teamtailor", egress)
    records = await adapter.run(
        PlanItem("ats.teamtailor", {"board_slugs": ["one", "two", "three"]})
    )
    assert egress.calls == [
        "https://one.teamtailor.com/jobs.rss",
        "https://two.teamtailor.com/jobs.rss",
        "https://three.teamtailor.com/jobs.rss",
    ]
    assert len(records) == 3 * 4


# ---------------------------------------------------------------------------
# Actiris
# ---------------------------------------------------------------------------


def test_actiris_drops_the_stale_tail_of_the_sitemap_and_orders_newest_first():
    """The index reaches back to 2023 and is not ordered.

    Taking it as it comes would spend a campaign's whole request budget - one
    request per offer - on adverts that closed years ago.
    """
    sitemap = fixture("boards", "actiris_sitemapoffers_nl.xml")
    today = datetime(2026, 9, 9, tzinfo=UTC)
    recent = ActirisAdapter.recent_offers(sitemap, max_age_days=60, today=today)
    assert [o["reference"] for o in recent] == [
        "5948560", "5948558", "5948557", "5948556", "5948551",
    ]
    everything = ActirisAdapter.recent_offers(sitemap, max_age_days=100_000, today=today)
    assert len(everything) == 8
    assert [o["lastmod"] for o in everything] == sorted(
        (o["lastmod"] for o in everything), reverse=True
    )


def test_actiris_reads_the_employer_off_the_how_to_apply_table():
    """The employer name is the whole point of this source (plan 2.3).

    25 of 25 sampled offers named a distinct employer; losing that field would
    make Actiris 23,000 anonymous adverts.
    """
    nl = ActirisAdapter().parse(actiris_record("nl"))[0]
    assert nl["company_name_raw"] == "ELIS BELGIUM"
    assert nl["title"] == "Account Manager M/V/X"
    assert nl["location"] == "1070 - Anderlecht"
    assert nl["country"] == "BE"
    assert nl["contract_type"] == "fixed_term"        # <main data-gtmContractType="CDD">
    assert nl["fte_percentage"] == 100                # icon-clock: "Voltijds"
    assert nl["function_family"].startswith("Handel")
    assert nl["posted_at"].startswith("2026-09-09")   # "Gecreëerd op 09 september 2026"
    assert nl["language"] == "nl"
    assert nl["application_target"].startswith("https://easyapply.jobs/")
    assert len(nl["description"]) > 500


def test_actiris_reads_the_language_neutral_contract_code_off_main():
    """``<main data-gtmContractType>`` is read, not the Dutch/French phrase.

    HTML attribute names are case-insensitive and the parser lowercases them,
    so an exact-key lookup for ``data-gtmContractType`` found nothing - and the
    failure was invisible because the Dutch phrase next to it happened to
    agree.  On a page where they disagree, the code is the one that must win.
    """
    record = actiris_record("nl")
    assert "Bepaalde duur" in record.content
    for code, expected in (("CDI", "permanent"), ("INTERIM", "interim")):
        mutated = actiris_record("nl")
        mutated.content = record.content.replace('data-gtmContractType="CDD"',
                                                 f'data-gtmContractType="{code}"')
        assert ActirisAdapter().parse(mutated)[0]["contract_type"] == expected


def test_actiris_takes_the_country_from_the_page_rather_than_assuming_one():
    """``<main data-gtmCountry>`` is the page's own statement.

    Hard-coding BE would be an assumption, and a Brussels-listed offer for a
    job over the Dutch border would be stored in the wrong country.
    """
    record = actiris_record("nl")
    assert ActirisAdapter().parse(record)[0]["country"] == "BE"
    record.content = record.content.replace('data-gtmCountry="Belgi&#235;"',
                                            'data-gtmCountry="Nederland"')
    assert ActirisAdapter().parse(record)[0]["country"] == "NL"


def test_actiris_parses_the_french_half_of_the_site_with_the_same_code():
    """The facts are read off the ``icon-*`` classes, not off the label text."""
    fr = ActirisAdapter().parse(actiris_record("fr"))[0]
    assert fr["company_name_raw"] == "TOISON GRILL"
    assert fr["location"] == "1050 - Ixelles"
    assert fr["contract_type"] == "fixed_term"
    assert fr["fte_percentage"] == 100                # "Temps plein"
    assert fr["language"] == "fr"
    assert fr["posted_at"].startswith("2026-09-09")   # "Créé le 09 septembre 2026"


async def test_actiris_reads_the_sitemap_once_and_then_one_advert_per_offer(db):
    load_all()
    egress = StubEgress(
        pages={
            "sitemapoffers-nl.xml": fixture("boards", "actiris_sitemapoffers_nl.xml"),
            "jobadvertentie": fixture("boards", "actiris_offer_nl.html"),
        }
    )
    adapter = get_adapter("board.actiris", egress)
    records = await adapter.run(
        PlanItem(
            "board.actiris",
            {"language": "nl", "page": 1, "offers_per_page": 3, "max_age_days": 100_000},
        )
    )
    assert egress.calls[0].endswith("sitemapoffers-nl.xml")
    assert len(egress.calls) == 4, "one index read plus one advert per offer"
    assert [c.rsplit("=", 1)[-1] for c in egress.calls[1:]] == ["5948560", "5948558", "5948557"]
    assert len(records) == 3
    assert all(r.entity_type == "vacancy" for r in records)


async def test_actiris_page_two_reads_the_next_slice_not_the_same_one(db):
    load_all()
    egress = StubEgress(
        pages={
            "sitemapoffers-nl.xml": fixture("boards", "actiris_sitemapoffers_nl.xml"),
            "jobadvertentie": fixture("boards", "actiris_offer_nl.html"),
        }
    )
    adapter = get_adapter("board.actiris", egress)
    await adapter.run(
        PlanItem(
            "board.actiris",
            {"language": "nl", "page": 2, "offers_per_page": 3, "max_age_days": 100_000},
        )
    )
    assert [c.rsplit("=", 1)[-1] for c in egress.calls[1:]] == ["5948556", "5948551", "4401218"]


async def test_actiris_adverts_that_all_fail_fail_the_plan_item(db):
    """FR-185: fifty 404s is a broken source, not a page with nothing on it."""
    load_all()
    egress = StubEgress(
        pages={"sitemapoffers-nl.xml": fixture("boards", "actiris_sitemapoffers_nl.xml")},
        default_status=404,
    )
    adapter = get_adapter("board.actiris", egress)
    with pytest.raises(SourceUnavailable):
        await adapter.run(
            PlanItem("board.actiris", {"language": "nl", "page": 1, "max_age_days": 100_000})
        )


async def test_actiris_robots_refusal_reaches_the_worker(db):
    """FR-182: robots.txt is the one answer that must never look like 'no jobs'."""
    load_all()
    egress = StubEgress(raises=RobotsDisallowed("robots.txt disallows www.actiris.brussels"))
    adapter = get_adapter("board.actiris", egress)
    with pytest.raises(RobotsDisallowed):
        await adapter.run(PlanItem("board.actiris", {"language": "nl", "page": 1}))


# ---------------------------------------------------------------------------
# Arbeitnow
# ---------------------------------------------------------------------------


def test_arbeitnow_reads_the_fields_the_live_api_actually_sends():
    rows = ArbeitnowAdapter().parse(arbeitnow_record())
    row = rows[0]
    assert row["company_name_raw"] == "Lexroom"
    assert row["title"].startswith("Customer Success Manager")
    assert row["location"] == "Berlin"
    assert row["country"] == "DE"
    assert row["function_family"] == "Customer Success"        # tags[0]
    assert row["contract_type"] == "permanent"                 # job_types ["Full Time"]
    assert row["posted_at"].startswith("2026-09-0")            # created_at, epoch seconds
    assert len(row["description"]) > 500


def test_arbeitnow_records_link_back_to_arbeitnow():
    """IR-101: "I would appreciate linking back to the site" is a term of use.

    Every stored row therefore carries the arbeitnow.com posting as both its
    source and its application route.
    """
    for row in ArbeitnowAdapter().parse(arbeitnow_record()):
        assert row["source_url"].startswith("https://www.arbeitnow.com/jobs/")
        assert row["application_target"] == row["source_url"]


def test_arbeitnow_states_its_terms_in_the_catalogue():
    notes = ArbeitnowAdapter.legal_notes.lower()
    assert "do not abuse" in notes
    assert "linking back" in notes


async def test_arbeitnow_sends_only_the_documented_page_parameter(db):
    """"Please do not abuse": ``page`` is the only parameter the API documents.

    Keyword filtering therefore happens on the rows that came back, not by
    guessing at query strings the service never advertised.
    """
    load_all()
    egress = StubEgress(
        pages={"job-board-api": fixture("boards", "arbeitnow_job_board_page1.json")}
    )
    adapter = get_adapter("board.arbeitnow", egress)
    records = await adapter.run(
        PlanItem(
            "board.arbeitnow",
            {"page": 1, "pages": 1, "keywords": ["Partner Manager"], "title_filter": True},
        )
    )
    assert egress.calls == ["https://www.arbeitnow.com/api/job-board-api?page=1"]
    assert egress.bodies == [{}], "a GET carries no body and no filter payload"
    assert [r.data["title"] for r in records] == ["Senior Partner Manager - DACH"]


async def test_arbeitnow_follows_its_own_next_link_and_then_stops(db):
    load_all()
    payload = json.loads(fixture("boards", "arbeitnow_job_board_page1.json"))
    assert payload["links"]["next"], "the captured page really does name a next link"
    last = json.loads(json.dumps(payload))
    last["links"]["next"] = None
    egress = StubEgress(pages={"page=1": json.dumps(payload), "page=2": json.dumps(last)})
    adapter = get_adapter("board.arbeitnow", egress)
    await adapter.run(PlanItem("board.arbeitnow", {"page": 1, "pages": 5}))
    assert egress.calls == [
        "https://www.arbeitnow.com/api/job-board-api?page=1",
        "https://www.arbeitnow.com/api/job-board-api?page=2",
    ]


def test_actiris_plans_one_search_rather_than_one_item_per_page():
    """FR-162, FR-186: the page belongs to collection, so an item may not carry one.

    These are the exact conditions that put twenty indistinguishable Actiris rows
    on the collection screen: a twenty-page budget turned into twenty items, each
    naming its own ``page``.  ``collection._run_page`` overwrites
    ``native_query["page"]`` with its own counter, so all twenty fetched offers
    1-50 - the same fifty adverts twenty times, and offers 51-1000 never read.
    One item asking for twenty pages of depth is the same budget spent on twenty
    different slices.
    """
    items = ActirisAdapter().plan({}, {}, {"max_pages_per_source": 20})
    assert len(items) == 1, "one search is one plan item, however deep it goes"
    assert "page" not in items[0].native_query
    assert items[0].estimated_pages == 20
    # The depth is not lost with the nineteen rows: the estimate still covers
    # twenty pages, so the review screen prices the same work (FR-163).
    assert items[0].estimated_seconds == 2 * (OFFERS_PER_PAGE + 1) * 20


def test_arbeitnow_bounds_one_plan_item_however_generous_the_caps_are():
    """One item for the feed, bounded by MAX_PAGES_PER_ITEM (IR-101).

    The page is collection's to set - it re-issues the item once per page (see
    ``vacancy_source.requested_page``) - so an item that named its own page was
    overwritten and nineteen of twenty such items were duplicate traffic against
    an API whose terms say "do not abuse".
    """
    items = ArbeitnowAdapter().plan({}, {}, {"max_pages_per_source": 10_000})
    assert len(items) == 1
    assert "page" not in items[0].native_query
    assert items[0].estimated_pages == 20
