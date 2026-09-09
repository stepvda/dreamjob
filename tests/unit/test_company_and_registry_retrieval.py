"""Retrieval regressions for registries, company sites and newsrooms.

Every test here pins a defect that made a source collect nothing while
reporting success.  Two rules follow from that and shape the file:

* the failures must now be **visible** - an adapter that cannot execute its
  plan item raises :class:`~dreamjob.adapters.query_errors.UnusableQuery`, which
  ``collection_worker`` records against that plan item, instead of returning an
  empty list that is written down as ``done, 0 records, 0 errors``;
* nothing touches the network.  The egress layer is a stub serving captured
  responses (``tests/fixtures/registries``) and trimmed copies of the real SEC,
  ECB and RSS payloads.

FR-181, FR-221, FR-225, FR-241..246, DR-101, DR-103, NFR-403, NFR-404.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from dreamjob.adapters import load_all
from dreamjob.adapters.base import PlanItem, get_adapter
from dreamjob.adapters.query_errors import UnusableQuery
from dreamjob.config import get_settings
from dreamjob.db.connection import insert_row, query_all, query_one
from dreamjob.db.migrator import migrate
from dreamjob.pipeline import knowledge_base

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "registries"


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DREAMJOB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DREAMJOB_DB_PATH", str(tmp_path / "data" / "test.db"))
    get_settings.cache_clear()
    get_settings()
    migrate()
    load_all()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


@dataclass
class StubResponse:
    url: str
    text: str
    status_code: int = 200
    #: No stored raw document: these tests exercise the record path, and a
    #: provenance row pointing at a document that does not exist is a FK error.
    raw_document_id: str | None = None
    headers: dict = field(default_factory=lambda: {"content-type": "text/html"})
    from_cache: bool = False

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    @property
    def content(self) -> bytes:
        return self.text.encode()


class StubEgress:
    """Serves captured payloads by URL fragment and records every request."""

    def __init__(self, pages: dict[str, str], default_status: int = 404):
        self.pages = pages
        self.calls: list[str] = []
        self.default_status = default_status

    async def fetch(self, url: str, **_kwargs: Any) -> StubResponse:
        self.calls.append(url)
        # Longest fragment wins, so a home-page entry does not shadow the feed
        # and the sub-pages that live under it.
        for fragment in sorted(self.pages, key=len, reverse=True):
            if fragment in url:
                return StubResponse(url=url, text=self.pages[fragment])
        return StubResponse(url=url, text="", status_code=self.default_status)

    async def __aenter__(self) -> StubEgress:
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        return False


def _use_stub_client(monkeypatch, egress: StubEgress) -> None:
    """Make ``EgressClient()`` hand back the stub, wherever it is opened."""
    from dreamjob.adapters.news import rss
    from dreamjob.adapters.website import crawler
    from dreamjob.egress import client as egress_module

    factory = lambda *_a, **_k: egress  # noqa: E731 - a one-line test double
    for module in (egress_module, crawler, rss):
        monkeypatch.setattr(module, "EgressClient", factory)


# Trimmed copies of the real payloads.
COMPANYFACTS = json.dumps(
    {
        "cik": 320193,
        "entityName": "Apple Inc.",
        "facts": {
            "us-gaap": {
                "RevenueFromContractWithCustomerExcludingAssessedTax": {
                    "units": {
                        "USD": [
                            {
                                "start": "2023-10-01", "end": "2024-09-28",
                                "val": 391_035_000_000, "form": "10-K",
                                "filed": "2024-11-01", "fy": 2024, "fp": "FY",
                            },
                            {
                                "start": "2022-09-25", "end": "2023-09-30",
                                "val": 383_285_000_000, "form": "10-K",
                                "filed": "2023-11-03", "fy": 2023, "fp": "FY",
                            },
                        ]
                    }
                },
                "StockholdersEquity": {
                    "units": {
                        "USD": [
                            {
                                "end": "2024-09-28", "val": 56_950_000_000, "form": "10-K",
                                "filed": "2024-11-01", "fy": 2024, "fp": "FY",
                            }
                        ]
                    }
                },
            }
        },
    }
)

SUBMISSIONS = json.dumps(
    {"cik": "320193", "name": "Apple Inc.", "sic": "3571", "tickers": ["AAPL"],
     "addresses": {"business": {"city": "Cupertino", "stateOrCountry": "CA"}}}
)

ECB_CSV = (
    "KEY,FREQ,CURRENCY,CURRENCY_DENOM,TIME_PERIOD,OBS_VALUE\n"
    "EXR.D.USD.EUR.SP00.A,D,USD,EUR,2024-09-27,1.1160\n"
)

SEC_PAGES = {
    "companyfacts": COMPANYFACTS,
    "submissions": SUBMISSIONS,
    "data-api.ecb.europa.eu": ECB_CSV,
}

PLANNER_REGISTRY_QUERY = {
    "country": "US",
    "legal_ids": [],
    "sector_codes": ["62"],
    "countries": ["US"],
    "page": 1,
}


def _seed_company(**overrides: Any) -> str:
    values = {
        "name": "Apple Inc.",
        "normalised_name": "apple",
        "country": "US",
        "jurisdiction": "US",
        "legal_id": "0000320193",
        "legal_id_type": "cik",
        "collected_at": "2026-09-01T00:00:00+00:00",
        "access_method": "api",
        "confidence": 0.5,
    }
    values.update(overrides)
    return insert_row("company", values)


# ---------------------------------------------------------------------------
# FR-241 / FR-181: a registry plan item that names no company
# ---------------------------------------------------------------------------


def test_a_registry_plan_item_that_resolves_to_no_company_fails_loudly(db):
    """It used to return [] and be written down as done / 0 records / 0 errors."""
    adapter = get_adapter("registry.sec_edgar", StubEgress(SEC_PAGES))
    item = PlanItem(adapter_key=adapter.key, native_query=dict(PLANNER_REGISTRY_QUERY))

    with pytest.raises(UnusableQuery) as raised:
        asyncio.run(adapter.run(item))

    message = str(raised.value)
    assert "no company" in message
    assert "legal_ids" in message, "the message has to name the key that is missing"


def test_a_registry_reads_the_companies_the_knowledge_base_already_holds(db):
    """FR-241/FR-342: the planner names no company, so the register asks the KB."""
    company_id = _seed_company()
    egress = StubEgress(SEC_PAGES)
    adapter = get_adapter("registry.sec_edgar", egress)

    records = asyncio.run(
        adapter.run(PlanItem(adapter_key=adapter.key, native_query=dict(PLANNER_REGISTRY_QUERY)))
    )
    written = knowledge_base.KnowledgeBaseWriter(adapter_key=adapter.key).write_many(records)

    assert any("companyfacts" in call for call in egress.calls), "the register was never asked"
    kinds = [r.entity_type for r in records]
    assert kinds.count("financial_year") == 2
    assert len(written) == len(records)
    years = query_all(
        "SELECT fiscal_year, revenue, currency, fx_rate_to_eur FROM financial_year "
        "WHERE company_id = ? ORDER BY fiscal_year",
        (company_id,),
    )
    assert [row["fiscal_year"] for row in years] == [2023, 2024]
    assert years[-1]["revenue"] == pytest.approx(391_035_000_000)


def test_the_collection_path_prices_a_filing_in_eur(db):
    """DR-103: fx belonged to the per-company path only, so collected years had none."""
    _seed_company()
    adapter = get_adapter("registry.sec_edgar", StubEgress(SEC_PAGES))
    records = asyncio.run(
        adapter.run(PlanItem(adapter_key=adapter.key, native_query=dict(PLANNER_REGISTRY_QUERY)))
    )

    years = [r for r in records if r.entity_type == "financial_year"]
    assert years, "the fixture carries two annual figures"
    for record in years:
        assert record.data["currency"] == "USD"
        rate = record.data["fx_rate_to_eur"]
        assert rate is not None, "a USD filing stored without a rate is read as parity"
        assert rate == pytest.approx(1 / 1.1160, rel=1e-6)


def test_a_filing_is_stored_even_when_the_company_row_does_not_exist_yet(db):
    """The registry half of the chicken-and-egg: no id meant the figures were dropped."""
    adapter = get_adapter("registry.sec_edgar", StubEgress(SEC_PAGES))
    item = PlanItem(
        adapter_key=adapter.key,
        native_query={
            "company": {"name": "Apple Inc.", "legal_id": "0000320193", "legal_id_type": "cik"},
            "years": 5,
            "page": 1,
        },
    )

    records = asyncio.run(adapter.run(item))
    knowledge_base.KnowledgeBaseWriter(adapter_key=adapter.key).write_many(records)

    assert query_one("SELECT COUNT(*) AS n FROM company")["n"] == 1
    stored = query_all("SELECT company_id, fiscal_year FROM financial_year")
    assert len(stored) == 2
    assert all(row["company_id"] for row in stored)


def test_a_registry_collect_without_a_client_opens_one(db, monkeypatch):
    """IR-102: ``egress=None`` used to make every request raise AttributeError."""
    from dreamjob.adapters.registries.sec_edgar import SECEdgarAdapter

    egress = StubEgress(SEC_PAGES)
    _use_stub_client(monkeypatch, egress)

    result = asyncio.run(
        SECEdgarAdapter().collect(
            {"name": "Apple Inc.", "legal_id": "0000320193", "legal_id_type": "cik"}
        )
    )

    assert egress.calls, "no request was issued at all"
    assert result.facts, "the register answered but the answer was thrown away"
    assert not any("not found" in note for note in result.notes)


# ---------------------------------------------------------------------------
# DR-101: the KBO name search
# ---------------------------------------------------------------------------


def test_the_kbo_name_search_sends_the_form_the_register_expects(db):
    """The old parameter set answered HTTP 404, so name resolution never worked."""
    from dreamjob.adapters.registries.kbo import KBOAdapter

    egress = StubEgress({"zoeknaamfonetischform": (FIXTURES / "kbo_name_search_barco.html").read_text()})
    number = asyncio.run(KBOAdapter().search_by_name("Barco", egress=egress))

    assert number == "0473191041"
    sent = egress.calls[0]
    for required in ("_ondNP=on", "_ondRP=on", "_vest=on", "actionNPRP=Search", "searchWord=Barco"):
        assert required in sent


def test_the_kbo_name_search_picks_the_entity_not_the_first_ten_digit_number(db):
    """A phonetic hit list is ranked; ``matches[0]`` anchored DR-101 on the wrong company."""
    from dreamjob.adapters.registries.kbo import KBOAdapter

    markup = (FIXTURES / "kbo_name_search_barco.html").read_text()
    rows = KBOAdapter.parse_search_results(markup)

    assert {"kind", "number", "name"} <= set(rows[0])
    assert rows[0]["number"] == "0822250687", "the leading zero is restored"
    # BAR COMPANY BELGIUM is the first registered entity on the page; BARCO NV is
    # the fourth row, and it is the one that matches the name that was asked for.
    assert KBOAdapter.pick_search_result(markup, "Barco") == "0473191041"
    assert KBOAdapter.pick_search_result(markup, "Some Other Company") is None


def test_the_kbo_company_page_yields_the_dr101_identity(db):
    """The register's own page is the identity anchor: number, VAT, seat, activities."""
    from dreamjob.adapters.registries.kbo import KBOAdapter

    markup = (FIXTURES / "kbo_company_0473191041.html").read_text()
    identity = KBOAdapter().parse_company_page(markup, "0473191041", {})

    assert identity["name"] == "BARCO"
    assert identity["legal_id"] == "0473191041"
    assert identity["vat_number"] == "BE0473191041"
    assert identity["locations"][0]["address"].startswith("President Kennedypark")
    assert "26.700" in identity["sector_codes"]


# ---------------------------------------------------------------------------
# FR-241: the NBB gateway
# ---------------------------------------------------------------------------


def test_the_nbb_asks_the_gateway_that_honours_the_subscription_key(db, monkeypatch):
    """consult.cbso.nbb.be answers 403 to everything and ignores the key."""
    from dreamjob.adapters.registries import nbb

    monkeypatch.setenv("NBB_CBSO_SUBSCRIPTION_KEY", "test-key")
    adapter = nbb.NBBAdapter()
    egress = StubEgress({"/legalEntity/": json.dumps([])})

    asyncio.run(adapter.collect({"legal_id": "0473191041"}, egress=egress))

    assert egress.calls == ["https://ws.cbso.nbb.be/authentic/legalEntity/0473191041/references"]
    assert nbb.accounting_data_url("REF1").endswith("/deposit/REF1/accountingData")
    assert "consult.cbso.nbb.be" not in nbb.CONSULT_BASE


# ---------------------------------------------------------------------------
# DR-103 / NFR-404: a rate that is not there is not parity
# ---------------------------------------------------------------------------


def test_a_non_eur_row_without_a_rate_is_never_read_as_parity():
    from dreamjob.pipeline import financial
    from dreamjob.pipeline.filing_extract import eur_rate, eur_values, unconverted_flag

    row = {"fiscal_year": 2024, "currency": "USD", "fx_rate_to_eur": None, "revenue": 391_035.0}

    assert eur_rate(row) is None
    assert eur_values(row)["revenue"] is None
    assert unconverted_flag(row)["code"] == "fx_rate_missing"

    figures = financial.to_year_figures([row])[0]
    assert figures.revenue is None
    assert figures.is_estimated is True
    assert any(flag["code"] == "fx_rate_missing" for flag in figures.flags)

    priced = {**row, "fx_rate_to_eur": 0.8962}
    assert financial.to_year_figures([priced])[0].revenue == pytest.approx(391_035.0 * 0.8962)
    assert financial.to_year_figures([priced])[0].is_estimated is False


# ---------------------------------------------------------------------------
# FR-185 / NFR-603: the financial slice is wired into something
# ---------------------------------------------------------------------------


def test_the_financial_job_worker_is_registered(db):
    """A worker nothing registers is a job kind that can never run."""
    from dreamjob.jobs.runner import runner
    from dreamjob.pipeline import financial  # noqa: F401 - importing is what registers it

    assert financial.JOB_KIND in runner._workers


def test_the_financials_stage_reaches_companies_provenance_does_not_know(db):
    """``provenance`` is empty whenever collection wrote nothing; the KB is not."""
    from dreamjob.pipeline import financial

    company_id = _seed_company()
    assert query_all("SELECT * FROM provenance") == []
    assert financial.campaign_companies("no-such-campaign") == [company_id]


def test_collect_company_financials_opens_its_own_client(db, monkeypatch):
    """The watchlist calls it with no egress; every registry call used to fail."""
    from dreamjob.pipeline import financial

    company_id = _seed_company()
    egress = StubEgress(SEC_PAGES)
    _use_stub_client(monkeypatch, egress)

    report = asyncio.run(
        financial.collect_company_financials(
            {"id": company_id, "name": "Apple Inc.", "country": "US",
             "legal_id": "0000320193", "legal_id_type": "cik"}
        )
    )

    assert egress.calls, "no registry request was issued"
    assert report["years_written"] == 2
    assert query_all("SELECT * FROM financial_year")


# ---------------------------------------------------------------------------
# FR-221: the company website
# ---------------------------------------------------------------------------

SITE_HOME = "https://acme-robotics.example"

SITE = {
    SITE_HOME: """
        <html><head><title>Acme Robotics</title>
        <link rel="alternate" type="application/rss+xml" href="/feed.xml">
        <script type="application/ld+json">
        {"@context":"https://schema.org","@type":"Organization","name":"Acme Robotics NV",
          "vatID":"BE0123456789"}
        </script></head>
        <body><main><h1>Acme Robotics</h1><p>We automate warehouses.</p>
        <a href="/who-we-are">Who we are</a>
        <a href="/who-we-are/careers">Working at Acme</a>
        <a href="/customer/cases/energy-company/back-pressure-forecasting">A case</a>
        </main></body></html>
    """,
    f"{SITE_HOME}/who-we-are": """
        <html><head><title>Who we are</title></head><body><main>
        Acme Robotics builds autonomous mobile robots in Ghent.</main></body></html>
    """,
    f"{SITE_HOME}/who-we-are/careers": """
        <html><head><title>Working at Acme</title></head><body><main>
        We are hiring: 12 open positions. Apply now through
        <a href="https://job-boards.greenhouse.io/acmerobotics">our board</a>.
        </main></body></html>
    """,
    f"{SITE_HOME}/customer/cases/energy-company/back-pressure-forecasting": """
        <html><head><title>Back-pressure forecasting</title></head><body><main>
        How we helped an energy company forecast back pressure.</main></body></html>
    """,
    f"{SITE_HOME}/feed.xml": """<?xml version="1.0"?>
        <rss version="2.0"><channel><title>Acme newsroom</title>
        <item><title>Acme raises EUR 18 million Series B</title>
              <link>https://acme-robotics.example/news/series-b</link>
              <pubDate>Mon, 02 Mar 2026 09:00:00 +0100</pubDate>
              <description>The round is led by a growth investor.</description></item>
        </channel></rss>
    """,
}

PLANNER_WEBSITE_QUERY = {"crawl_seeds": [], "paths": ["/about", "/careers"], "page": 1}
PLANNER_NEWS_QUERY = {
    "query": "AI OR data OR software",
    "companies": [],
    "since_days": 90,
    "countries": ["BE"],
    "page": 1,
}


def _seed_site_company(**overrides: Any) -> str:
    values = {
        "name": "Acme Robotics",
        "normalised_name": "acme robotics",
        "domain": "acme-robotics.example",
        "country": "BE",
        "collected_at": "2026-01-01T00:00:00+00:00",
        "access_method": "http",
        "confidence": 0.4,
    }
    values.update(overrides)
    return insert_row("company", values)


def test_a_careers_page_filed_under_another_kind_is_still_the_careers_page(db):
    """``/who-we-are/careers`` scores higher as an *about* page, and was lost."""
    from dreamjob.adapters.website import crawler

    url = f"{SITE_HOME}/who-we-are/careers"
    assert crawler.classify_url(url)[0] == "about"
    assert crawler.is_careers_url(url) is True

    result = asyncio.run(
        crawler.crawl_site(SITE_HOME, max_pages=6, max_depth=2, egress=StubEgress(SITE))
    )
    assert result.careers_url == url
    assert result.ats_vendor == "greenhouse"


def test_a_case_study_no_longer_takes_the_about_quota(db):
    """Keywords are matched on whole path segments, not anywhere in the path."""
    from dreamjob.adapters.website import crawler

    case = "https://www.ml6.eu/en/customer/cases/energy-company/back-pressure-forecasting"
    kind, _score = crawler.classify_url(case)
    assert kind == "customers", "'company' inside a slug used to make this an about page"

    episode = (
        "https://www.raccoons.be/radio-raccoons/"
        "s06e10-over-apples-small-language-models-mai-1-en-de-ai-product-race"
    )
    assert crawler.classify_url(episode)[1] < crawler.classify_url(f"{SITE_HOME}/products")[1]


def test_the_website_adapter_reads_the_query_the_planner_actually_writes(db):
    """The planner writes ``crawl_seeds``; the adapter read ``url`` and fetched nothing."""
    adapter = get_adapter("website.crawl", StubEgress(SITE))

    with pytest.raises(UnusableQuery) as raised:
        asyncio.run(
            adapter.run(PlanItem(adapter_key=adapter.key, native_query=dict(PLANNER_WEBSITE_QUERY)))
        )
    assert "crawl_seeds" in str(raised.value)

    records = asyncio.run(
        adapter.run(
            PlanItem(
                adapter_key=adapter.key,
                native_query={**PLANNER_WEBSITE_QUERY, "crawl_seeds": [SITE_HOME], "max_pages": 6},
            )
        )
    )
    written = knowledge_base.KnowledgeBaseWriter(adapter_key=adapter.key).write_many(records)
    assert written, "a crawl of a real site has to produce a company record"
    company = query_one("SELECT * FROM company")
    assert company["domain"] == "acme-robotics.example"
    assert company["careers_url"] == f"{SITE_HOME}/who-we-are/careers"
    assert company["ats_vendor"] == "greenhouse"


def test_the_website_adapter_falls_back_to_the_companies_the_kb_holds(db):
    """A campaign whose plan item names no site still deepens what discovery found."""
    _seed_site_company()
    egress = StubEgress(SITE)
    adapter = get_adapter("website.crawl", egress)

    records = asyncio.run(
        adapter.run(
            PlanItem(
                adapter_key=adapter.key,
                native_query={**PLANNER_WEBSITE_QUERY, "countries": ["BE"], "max_pages": 4},
            )
        )
    )

    assert any("acme-robotics.example" in call for call in egress.calls)
    assert records, "the crawl produced nothing for a company it knows the domain of"


# ---------------------------------------------------------------------------
# FR-225: the newsroom
# ---------------------------------------------------------------------------


def test_the_news_adapter_reads_the_query_the_planner_actually_writes(db):
    adapter = get_adapter("news.rss", StubEgress(SITE))

    with pytest.raises(UnusableQuery) as raised:
        asyncio.run(
            adapter.run(PlanItem(adapter_key=adapter.key, native_query=dict(PLANNER_NEWS_QUERY)))
        )
    assert "newsroom" in str(raised.value)

    company_id = _seed_site_company()
    records = asyncio.run(
        get_adapter("news.rss", StubEgress(SITE)).run(
            PlanItem(adapter_key=adapter.key, native_query=dict(PLANNER_NEWS_QUERY))
        )
    )
    knowledge_base.KnowledgeBaseWriter(adapter_key=adapter.key).write_many(records)

    signals = query_all("SELECT * FROM hiring_signal WHERE company_id = ?", (company_id,))
    assert [s["signal_type"] for s in signals] == ["funding"]
    assert signals[0]["occurred_at"].startswith("2026-03-02")


def test_a_news_item_is_attached_to_its_company_by_the_feed_domain(db):
    """``hiring_signal.company_id`` is NOT NULL, so an unattached item is dropped."""
    company_id = _seed_site_company()
    adapter = get_adapter("news.rss", StubEgress(SITE))

    records = asyncio.run(
        adapter.run(
            PlanItem(
                adapter_key=adapter.key,
                # A url with no company_id: exactly what a hand-written or
                # crawl-derived plan item carries.
                native_query={"url": SITE_HOME, "company_id": None, "page": 1},
            )
        )
    )

    assert [r.data["company_id"] for r in records] == [company_id]


# ---------------------------------------------------------------------------
# FR-225 / FR-226: signals and the profile stage
# ---------------------------------------------------------------------------


def test_refresh_signals_reads_the_home_page_not_the_adapter_key(db, monkeypatch):
    """``company.source`` is an adapter key for every collected company."""
    from dreamjob.adapters.news import rss
    from dreamjob.pipeline import signals

    company_id = _seed_site_company(source="board.jobat")
    company = query_one("SELECT * FROM company WHERE id = ?", (company_id,))
    asked: list[str] = []

    async def _fake_collect(home_url: str, **_kwargs: Any) -> list:
        asked.append(home_url)
        return []

    monkeypatch.setattr(rss, "collect_news", _fake_collect)
    asyncio.run(signals.refresh_signals(company, pages=[]))

    assert asked == ["https://acme-robotics.example"]


def test_a_careers_page_filed_as_about_still_yields_a_postings_signal(db):
    from dreamjob.pipeline import signals

    company_id = _seed_site_company()
    page = {
        "kind": "about",
        "url": f"{SITE_HOME}/who-we-are/careers",
        "text": "We are hiring: 12 open positions across engineering.",
    }

    detected = signals.signals_from_pages(company_id, [page])
    assert "postings" in {s["signal_type"] for s in detected}


def test_a_degraded_reprofile_keeps_the_provenance_it_cannot_replace(db, monkeypatch):
    """NFR-402: an empty synthesis used to delete every field source and confidence."""
    from dreamjob.db.repositories import companies as repo
    from dreamjob.pipeline import company_profile

    company_id = _seed_site_company()
    repo.record_field_provenance(
        company_id,
        {"business_summary": {"confidence": 0.8}, "products_services": {"confidence": 0.7}},
    )
    assert len(repo.field_provenance(company_id)) == 2

    # NFR-104 degradation: no LLM, and a site whose home page carries nothing
    # deterministic that the company row does not already hold.  ``_synthesise``
    # returns ({}, False) on BudgetExhausted and LLMError as well - same state.
    plain = {
        SITE_HOME: (
            "<html><head><title>Acme Robotics</title></head>"
            "<body><main>We automate warehouses.</main></body></html>"
        )
    }
    company = query_one("SELECT * FROM company WHERE id = ?", (company_id,))
    outcome = asyncio.run(
        company_profile.build_profile(
            company, force=True, use_llm=False, max_pages=4, max_depth=2, egress=StubEgress(plain)
        )
    )

    assert outcome.reused is False
    assert outcome.fields_written == [], "this run is the degraded one; it wrote no field"
    assert len(repo.field_provenance(company_id)) == 2, "a degraded run erased NFR-402 provenance"


def test_the_company_profile_stage_profiles_the_campaigns_companies(db, monkeypatch):
    """The stage collection.py reserves for this module had no ``rerun`` at all."""
    from dreamjob.pipeline import collection, company_profile

    assert "company_profile" in {stage["stage"] for stage in collection.available_stages()}

    company_id = _seed_site_company()
    _use_stub_client(monkeypatch, StubEgress(SITE))

    report = asyncio.run(
        company_profile.rerun("campaign-1", "seeker-1", use_llm=False, max_pages=5, force=True)
    )

    assert report["companies"] == 1
    assert report["profiled"] == 1
    from dreamjob.db.repositories import companies as repo

    assert repo.crawled_pages(company_id), "the stage did not crawl the company site"
    assert query_all("SELECT * FROM hiring_signal WHERE company_id = ?", (company_id,))


def test_a_registry_without_its_key_says_so_on_the_plan_item(db, monkeypatch):
    """RK-06 degrades the figures, not the report: 'done, 0 records' hid the gap."""
    monkeypatch.delenv("NBB_CBSO_SUBSCRIPTION_KEY", raising=False)
    adapter = get_adapter("registry.nbb", StubEgress({}))

    with pytest.raises(UnusableQuery) as raised:
        asyncio.run(
            adapter.run(
                PlanItem(
                    adapter_key=adapter.key,
                    native_query={"country": "BE", "legal_ids": [], "page": 1},
                )
            )
        )

    assert "NBB_CBSO_SUBSCRIPTION_KEY" in str(raised.value)
