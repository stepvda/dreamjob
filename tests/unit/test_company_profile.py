"""Company crawl, profiling, competitors and signals (FR-221..226, FR-384, FR-402).

Everything here runs against a throw-away SQLite file with no network and no
LLM.  The website crawl is driven by a stub egress client serving an in-memory
site, so the URL scorer, the page budget and the deterministic extraction paths
are exercised exactly as they run in production; the LLM synthesis path is
switched off with ``use_llm=False``, which is also its NFR-104 degradation path.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from dreamjob.adapters.news import rss
from dreamjob.adapters.website import crawler
from dreamjob.config import get_settings
from dreamjob.db.connection import from_json, insert_row, query_all, utcnow
from dreamjob.db.migrator import migrate
from dreamjob.db.repositories import companies as repo
from dreamjob.pipeline import company_profile, competitors, signals


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DREAMJOB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DREAMJOB_DB_PATH", str(tmp_path / "data" / "test.db"))
    get_settings.cache_clear()
    get_settings()
    migrate()
    yield
    get_settings.cache_clear()


def _iso(days_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# A stub site and a stub egress client
# ---------------------------------------------------------------------------

HOME = "https://acme-robotics.example"

JSONLD = """
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Organization","name":"Acme Robotics NV",
 "legalName":"Acme Robotics NV","vatID":"BE0123456789","numberOfEmployees":{"value":240},
 "address":{"@type":"PostalAddress","streetAddress":"Havenlaan 12","postalCode":"9000",
            "addressLocality":"Ghent","addressCountry":"BE"}}
</script>
"""

SITE: dict[str, str] = {
    f"{HOME}": f"""
        <html><head><title>Acme Robotics - warehouse automation</title>
        <link rel="alternate" type="application/rss+xml" href="/feed.xml">
        {JSONLD}</head>
        <body><nav><a href="/privacy">Privacy</a><a href="/tag/news">Tags</a></nav>
        <main><h1>Acme Robotics</h1><p>We automate warehouses.</p>
        <a href="/about">About us</a>
        <a href="/products">Products and services</a>
        <a href="/customers">Our customers</a>
        <a href="/team">Leadership team</a>
        <a href="/careers">Careers</a>
        <a href="/news">Newsroom</a>
        <a href="/contact">Contact and offices</a>
        <a href="/values">Our values</a>
        <a href="/es/sobre-nosotros">Espanol</a>
        <a href="/cart">Basket</a>
        </main></body></html>
    """,
    f"{HOME}/about": """
        <html><head><title>About Acme Robotics</title></head><body><main>
        Acme Robotics builds autonomous mobile robots for warehouse operators in
        the Benelux. Founded in 2012 in Ghent.</main></body></html>
    """,
    f"{HOME}/products": """
        <html><head><title>Products</title></head><body><main>
        Autonomous mobile robots, fleet orchestration software and a warehouse
        execution platform.</main></body></html>
    """,
    f"{HOME}/customers": """
        <html><head><title>Customers</title></head><body><main>
        Trusted by Colruyt Group and Barco.</main></body></html>
    """,
    f"{HOME}/team": """
        <html><head><title>Leadership</title></head><body><main>
        An Devos, Chief Executive Officer. Peter Maes, VP Engineering.</main></body></html>
    """,
    f"{HOME}/careers": """
        <html><head><title>Careers</title></head><body><main>
        We are hiring across engineering and operations.
        <a href="https://boards.greenhouse.io/acmerobotics">See all open positions</a>
        </main></body></html>
    """,
    f"{HOME}/news": """
        <html><head><title>Newsroom</title></head><body><main>
        Acme Robotics raises EUR 18 million in a Series B funding round.</main></body></html>
    """,
    f"{HOME}/contact": """
        <html><head><title>Contact</title></head><body><main>
        Ghent (HQ), Eindhoven office.</main></body></html>
    """,
    f"{HOME}/values": """
        <html><head><title>Our values</title></head><body><main>
        Autonomy, craftsmanship and a bias to ship.</main></body></html>
    """,
    f"{HOME}/es/sobre-nosotros": "<html><body>Acerca de nosotros</body></html>",
    f"{HOME}/privacy": "<html><body>Privacy policy</body></html>",
    f"{HOME}/cart": "<html><body>Basket</body></html>",
    f"{HOME}/tag/news": "<html><body>Tagged</body></html>",
}


@dataclass
class StubFetch:
    url: str
    text: str
    status_code: int = 200
    headers: dict = field(default_factory=lambda: {"content-type": "text/html"})
    from_cache: bool = False
    raw_document_id: str | None = None
    content_hash: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


class StubEgress:
    """Serves the in-memory site and records what was asked for."""

    def __init__(self, pages: dict[str, str], store_raw: bool = True):
        self.pages = pages
        self.requested: list[str] = []
        self.store_raw = store_raw

    async def fetch(self, url: str, **kwargs) -> StubFetch:
        self.requested.append(url)
        # The crawler normalises URLs before fetching, so "…/" and "…" are one page.
        if url not in self.pages:
            alternative = url[:-1] if url.endswith("/") else url + "/"
            if alternative in self.pages:
                url = alternative
            else:
                raise RuntimeError(f"404 {url}")
        document_id = None
        if self.store_raw:
            document_id = insert_row(
                "raw_document",
                {
                    "url": url,
                    "content_type": "text/html",
                    "content_hash": f"hash-{len(self.requested)}-{abs(hash(url))}",
                    "storage_path": f"raw/{abs(hash(url))}.html",
                    "byte_size": len(self.pages[url]),
                    "http_status": 200,
                    "access_method": "http",
                    "fetched_at": utcnow(),
                },
            )
        return StubFetch(url=url, text=self.pages[url], raw_document_id=document_id)


def _seed_company(**overrides) -> dict:
    values = {
        "name": "Acme Robotics",
        "normalised_name": "acme robotics",
        "domain": "acme-robotics.example",
        "country": "BE",
        "collected_at": _iso(400),
        "access_method": "http",
        "confidence": 0.4,
    }
    values.update(overrides)
    company_id = insert_row("company", values)
    return repo.get_company(company_id)


# ---------------------------------------------------------------------------
# FR-221: the URL scorer and the bounded crawl
# ---------------------------------------------------------------------------


def test_url_scorer_ranks_the_pages_fr221_asks_for():
    domain = "acme-robotics.example"

    about = crawler.score_url(f"{HOME}/about", anchor="About us", depth=1, base_domain=domain)
    customers = crawler.score_url(f"{HOME}/customers", depth=1, base_domain=domain)
    deep_blog = crawler.score_url(f"{HOME}/blog/2019/03/page/7", depth=3, base_domain=domain)

    assert about > 0.7
    assert customers > 0.7
    assert about > deep_blog

    # Boilerplate, other sites and binaries never buy a page of the budget.
    assert crawler.score_url(f"{HOME}/privacy", base_domain=domain) < 0
    assert crawler.score_url(f"{HOME}/tag/news", base_domain=domain) < 0
    assert crawler.score_url("https://facebook.com/acme", base_domain=domain) < 0
    assert crawler.score_url(f"{HOME}/brochure.jpg", base_domain=domain) < 0

    # A translation of a page we can already read is worth less than the original.
    assert crawler.score_url(f"{HOME}/es/sobre-nosotros", depth=1, base_domain=domain) < about


def test_crawl_stays_within_budget_and_classifies_pages(db):
    egress = StubEgress(SITE)
    result = asyncio.run(crawler.crawl_site(HOME, max_pages=8, max_depth=2, egress=egress))

    assert len(result.pages) <= 8
    kinds = result.kinds()
    assert kinds.get("home") == 1
    # The budget went on the page kinds FR-221 enumerates, not on boilerplate.
    assert {"about", "products", "customers"} <= set(kinds)
    assert f"{HOME}/privacy" not in egress.requested
    assert f"{HOME}/cart" not in egress.requested
    assert f"{HOME}/tag/news" not in egress.requested

    # Deterministic finds that need no LLM.
    assert f"{HOME}/feed.xml" in result.feeds
    identity = crawler.identity_from_jsonld(result.jsonld)
    assert identity["vat_number"] == "BE0123456789"
    assert identity["size_fte"] == 240
    assert identity["locations"][0]["city"] == "Ghent"


def test_crawl_finds_the_careers_page_and_its_ats(db):
    result = asyncio.run(
        crawler.crawl_site(HOME, max_pages=12, max_depth=2, egress=StubEgress(SITE))
    )
    assert result.careers_url == f"{HOME}/careers"
    assert result.ats_vendor == "greenhouse"
    assert result.ats_slug == "acmerobotics"


# ---------------------------------------------------------------------------
# FR-222 / FR-226 / NFR-402: assembly, provenance and reuse
# ---------------------------------------------------------------------------


def test_profile_assembly_without_llm(db):
    company = _seed_company()
    outcome = asyncio.run(
        company_profile.build_profile(
            company, use_llm=False, max_pages=10, max_depth=2, egress=StubEgress(SITE)
        )
    )

    assert outcome.reused is False
    assert outcome.llm_used is False
    assert outcome.pages_crawled > 3

    stored = repo.get_company(company["id"])
    assert stored["vat_number"] == "BE0123456789"
    assert stored["size_fte"] == 240
    assert stored["careers_url"] == f"{HOME}/careers"
    assert stored["ats_vendor"] == "greenhouse"
    assert stored["refreshed_at"] is not None
    assert from_json(stored["locations"])[0]["city"] == "Ghent"

    # NFR-402: every written field carries a confidence and a provenance link.
    provenance = repo.field_provenance(company["id"])
    assert {p["field_path"] for p in provenance} >= {"vat_number", "careers_url"}
    assert all(p["confidence"] is not None for p in provenance)

    # FR-221/FR-341: the page inventory survives in the knowledge base.
    pages = repo.crawled_pages(company["id"])
    assert len(pages) == outcome.pages_crawled
    assert all(p["field_path"].startswith(repo.PAGE_PROVENANCE_PREFIX) for p in pages)
    assert repo.last_crawl_at(company["id"]) is not None


def test_fresh_profile_is_reused_and_a_stale_one_is_recrawled(db):
    fresh = _seed_company(
        business_summary="Warehouse automation for Benelux retailers.",
        collected_at=utcnow(),
        refreshed_at=utcnow(),
    )
    egress = StubEgress(SITE)
    outcome = asyncio.run(company_profile.build_profile(fresh, use_llm=False, egress=egress))
    assert outcome.reused is True
    assert egress.requested == []

    stale = repo.get_company(fresh["id"])
    stale = dict(stale, refreshed_at=_iso(400), collected_at=_iso(400))
    assert company_profile.needs_refresh(stale) is True

    forced = asyncio.run(
        company_profile.build_profile(
            fresh, force=True, use_llm=False, max_pages=6, egress=StubEgress(SITE)
        )
    )
    assert forced.reused is False
    assert forced.pages_crawled > 0


def test_standardised_profile_exposes_the_fixed_schema(db):
    company = _seed_company()
    asyncio.run(
        company_profile.build_profile(
            company, use_llm=False, max_pages=8, egress=StubEgress(SITE)
        )
    )
    profile = company_profile.standardised_profile(company["id"])

    for section in (
        "identity", "business_summary", "sector_codes", "size", "locations", "structure",
        "key_people", "references", "financial_summary", "hiring_signals", "competitors",
        "values_culture", "sources", "confidence", "freshness",
    ):
        assert section in profile
    assert profile["identity"]["vat_number"] == "BE0123456789"
    assert profile["sources"], "the profile must cite the pages it was built from"
    assert profile["financial_summary"]["available"] is False


def test_dated_indicators_from_the_crawl_become_hiring_signals(db):
    """FR-225: what the site reports, with its date and URL, is a signal."""
    company = _seed_company(
        news=[
            {
                "title": "Acme opens a second site in Eindhoven",
                "signal_type": "new_office",
                "occurred_at": "2026-08-01",
                "url": f"{HOME}/news/eindhoven",
            },
            # A headline with no type and no date stays a display string.
            {"title": "Newsroom", "url": f"{HOME}/news", "signal_type": None},
        ]
    )
    detected = signals.signals_from_profile_news(company)

    assert [s["signal_type"] for s in detected] == ["new_office"]
    assert detected[0]["occurred_at"].startswith("2026-08-01")
    assert detected[0]["source_url"] == f"{HOME}/news/eindhoven"
    assert signals.detect_all(company) and any(
        s["signal_type"] == "new_office" for s in signals.detect_all(company)
    )


def test_peers_named_on_the_site_are_suggested(db):
    """FR-224: a comparable provider the pages name is an observation, not a guess."""
    target = _seed_company()
    peer = _seed_company(name="Robotiq Systems", normalised_name="robotiq systems")

    suggestions = competitors.suggest_competitors(
        target,
        use_llm=False,
        mentioned=[
            {"name": "Robotiq Systems", "basis": "product", "evidence": "named as a comparable"},
            {"name": "Nowhere Robotics", "basis": "press", "confidence": 0.4},
            {"name": "Robotiq Systems", "basis": "product"},  # de-duplicated
        ],
    )
    by_name = {s["name"]: s for s in suggestions}

    resolved = by_name["Robotiq Systems"]
    assert resolved["peer_company_id"] == peer["id"]
    assert "product" in resolved["basis_strengths"]
    # An unknown name is still offered, so the job seeker can adopt it.
    assert by_name["Nowhere Robotics"]["peer_company_id"] is None
    # The site's own framing never outranks an overlap two passes observed.
    assert max(resolved["basis_strengths"].values()) <= competitors.MENTION_STRENGTH_CEILING
    assert repo.list_competitors(target["id"])


def test_refresh_reuses_the_crawl_instead_of_fetching_the_site_twice(db):
    company = _seed_company()
    egress = StubEgress(SITE)
    outcome = asyncio.run(
        company_profile.build_profile(
            company, use_llm=False, max_pages=10, max_depth=2, egress=egress
        )
    )

    assert outcome.crawl is not None
    assert f"{HOME}/feed.xml" in outcome.feeds

    # The signal detectors read the pages this pass already fetched.
    detected = signals.detect_all(
        repo.get_company(company["id"]), pages=list(outcome.crawl.pages), news_items=[]
    )
    assert "postings" in {s["signal_type"] for s in detected}


def test_size_band_and_departmental_map_threshold():
    assert company_profile.size_band_for(7) == "1-10"
    assert company_profile.size_band_for(240) == "201-500"
    assert company_profile.size_band_for(9000) == "5000+"
    assert company_profile.size_band_for(None) is None
    assert company_profile.DEPARTMENT_MAP_MIN_FTE > 0


# ---------------------------------------------------------------------------
# FR-224: competitors from combined signals
# ---------------------------------------------------------------------------


def test_competitor_suggestions_combine_several_signals(db):
    target = _seed_company(
        sector_codes=[{"system": "nace", "code": "6201", "label": "software"}],
        reference_customers=[{"name": "Colruyt Group"}, {"name": "Barco"}],
        business_summary=(
            "Autonomous mobile robots and fleet orchestration software for warehouse "
            "operators, with a warehouse execution platform used by retailers."
        ),
        products_services=[{"name": "fleet orchestration", "description": "robot routing"}],
        size_band="201-500",
    )
    sector_peer = _seed_company(
        name="Beta Automation",
        normalised_name="beta automation",
        domain="beta.example",
        sector_codes=[{"system": "nace", "code": "6201", "label": "software"}],
        size_band="201-500",
        business_summary="Industrial control panels.",
        collected_at=utcnow(),
    )
    customer_peer = _seed_company(
        name="Gamma Intralogistics",
        normalised_name="gamma intralogistics",
        domain="gamma.example",
        reference_customers=[{"name": "Colruyt Group"}],
        business_summary="Conveyor engineering.",
        collected_at=utcnow(),
    )
    product_peer = _seed_company(
        name="Delta Robotics",
        normalised_name="delta robotics",
        domain="delta.example",
        business_summary=(
            "Autonomous mobile robots and orchestration software for warehouse operators "
            "and retailers, including a warehouse execution platform."
        ),
        collected_at=utcnow(),
    )

    suggestions = competitors.suggest_competitors(target, use_llm=False)

    # The acceptance criteria ask for at least three suggestions per company.
    assert len(suggestions) >= 3
    by_id = {s["peer_company_id"]: s for s in suggestions}
    assert "sector" in by_id[sector_peer["id"]]["bases"]
    assert "customers" in by_id[customer_peer["id"]]["bases"]
    assert "product" in by_id[product_peer["id"]]["bases"]
    assert all(s["strength"] > 0 for s in suggestions)

    # Every basis is stored as its own link, so the reason survives (FR-224).
    links = repo.list_competitors(target["id"])
    assert {row["basis"] for row in links} >= {"sector", "customers", "product"}

    # Re-running merges rather than duplicating.
    competitors.suggest_competitors(target, use_llm=False)
    assert len(repo.list_competitors(target["id"])) == len(links)

    stored = competitors.stored_suggestions(target["id"])
    assert len(stored) >= 3


def test_adopting_a_named_peer_creates_it_and_puts_it_on_the_target_list(db):
    seeker_id = insert_row(
        "job_seeker",
        {
            "email": "seeker@example.com",
            "display_name": "Test Seeker",
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )
    target = _seed_company()
    link_id = repo.upsert_competitor_link(
        target["id"], basis="sector", strength=0.4, peer_name="Epsilon Systems"
    )

    result = competitors.adopt(seeker_id, link_id)

    assert result["created_company"] is True
    peer = repo.get_company(result["company_id"])
    assert peer["name"] == "Epsilon Systems"
    assert repo.is_watchlisted(seeker_id, peer["id"]) is True
    # Adopting twice is idempotent.
    assert competitors.adopt(seeker_id, link_id)["company_id"] == peer["id"]


# ---------------------------------------------------------------------------
# FR-225 / FR-402: signals and the application window
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("headline", "expected"),
    [
        ("Acme raises €18 million in a Series B funding round", "funding"),
        ("Acme opens a new office in Eindhoven", "new_office"),
        ("Acme launches its warehouse execution platform", "product_launch"),
        ("Acme appoints Jan Peeters as CFO", "leadership_change"),
        ("Acme acquires Beta Automation", "reorg"),
        ("Rival cuts 200 jobs in restructuring", "reorg"),
        ("Acme publishes its sustainability report", None),
    ],
)
def test_news_classification(headline, expected):
    signal_type, strength = signals.classify_news(headline)
    if expected is None:
        assert signal_type is None
    else:
        assert signal_type == expected
        assert 0 < strength <= 1


def test_timing_window_reflects_recent_funding(db):
    company = _seed_company()
    repo.upsert_signal(
        {
            "company_id": company["id"],
            "signal_type": "funding",
            "description": "Series B, EUR 18 million",
            "occurred_at": _iso(30),
            "source_url": f"{HOME}/news",
            "strength": 0.9,
        }
    )

    recommendation = signals.recommend_window(company["id"])
    assert recommendation.timing_flag in ("apply_now", "favourable")
    assert recommendation.score > 0.4
    assert recommendation.window_end is not None
    assert recommendation.drivers[0]["signal_type"] == "funding"
    assert "funding" in recommendation.rationale
    assert signals.timing_flag_for(company["id"]) == recommendation.timing_flag


def test_a_signal_whose_window_has_not_opened_says_wait(db):
    company = _seed_company()
    repo.upsert_signal(
        {
            "company_id": company["id"],
            "signal_type": "funding",
            "description": "Series A closed yesterday",
            "occurred_at": _iso(1),
            "source_url": f"{HOME}/news",
            "strength": 0.9,
        }
    )
    recommendation = signals.recommend_window(company["id"])
    assert recommendation.timing_flag == "wait"
    assert recommendation.window_start is not None
    assert signals.timing_flag_for(company["id"]) is None


def test_expired_signals_do_not_flag_a_company(db):
    company = _seed_company()
    repo.upsert_signal(
        {
            "company_id": company["id"],
            "signal_type": "funding",
            "description": "Series A, two years ago",
            "occurred_at": _iso(730),
            "source_url": f"{HOME}/news",
            "strength": 0.9,
        }
    )
    recommendation = signals.recommend_window(company["id"])
    assert recommendation.timing_flag == "neutral"
    assert recommendation.score == 0.0
    assert signals.timing_flag_for(company["id"]) is None


def test_signals_are_detected_from_pages_filings_and_postings(db):
    company = _seed_company()
    for year, fte in ((2023, 100.0), (2024, 140.0)):
        insert_row(
            "financial_year",
            {
                "company_id": company["id"],
                "fiscal_year": year,
                "period_end": f"{year}-12-31",
                "headcount_fte": fte,
                "collected_at": utcnow(),
            },
        )
    for index in range(4):
        insert_row(
            "vacancy",
            {
                "company_id": company["id"],
                "title": f"Engineer {index}",
                "function_family": f"family-{index}",
                "posted_at": _iso(5),
                "collected_at": utcnow(),
            },
        )

    crawl = asyncio.run(crawler.crawl_site(HOME, max_pages=10, egress=StubEgress(SITE)))
    detected = signals.detect_all(company, pages=crawl.pages, news_items=[])
    kinds = {s["signal_type"] for s in detected}

    assert "headcount_growth" in kinds
    assert "postings" in kinds
    assert "fiscal_year_start" in kinds
    # FR-225 asks for a date and a source.  A dated event carries `occurred_at`;
    # a standing statement on a page carries the URL it stands on and is dated
    # by `collected_at` when it is stored.
    assert all(s.get("occurred_at") or s.get("source_url") for s in detected)

    signals.persist(detected)
    stored = repo.list_signals(company["id"])
    assert len(stored) == len(detected)
    assert all(s["occurred_at"] or s["collected_at"] for s in stored)

    # A second pass a clock tick later is the same evidence, not new evidence.
    time.sleep(1.1)
    signals.persist(signals.detect_all(company, pages=crawl.pages, news_items=[]))
    assert len(repo.list_signals(company["id"])) == len(stored), "re-detection must not duplicate"


def test_refresh_signals_without_a_newsroom_degrades_gracefully(db):
    company = _seed_company()
    stored = asyncio.run(signals.refresh_signals(company, pages=[], fetch_news=False))
    # No news, no filings, no postings: a fiscal-year signal is still honest.
    assert {s["signal_type"] for s in stored} == {"fiscal_year_start"}
    assert query_all("SELECT * FROM hiring_signal WHERE company_id = ?", (company["id"],))


# ---------------------------------------------------------------------------
# The news adapter
# ---------------------------------------------------------------------------

RSS_XML = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>Acme newsroom</title>
<item><title>Acme raises EUR 18 million Series B</title>
      <link>https://acme-robotics.example/news/series-b</link>
      <pubDate>Mon, 02 Mar 2026 09:00:00 +0100</pubDate>
      <description>&lt;p&gt;The round is led by a growth investor.&lt;/p&gt;</description></item>
<item><title>Acme opens a new office in Eindhoven</title>
      <link>https://acme-robotics.example/news/eindhoven</link>
      <pubDate>Tue, 14 Jan 2026 08:00:00 +0100</pubDate>
      <description>A second location for the Dutch market.</description></item>
</channel></rss>
"""

ATOM_XML = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Acme</title>
<entry><title>Acme appoints a new CFO</title>
  <link rel="alternate" href="https://acme-robotics.example/news/cfo"/>
  <updated>2026-02-10T12:00:00Z</updated>
  <summary>Jan Peeters joins as Chief Financial Officer.</summary></entry>
</feed>
"""


def test_rss_and_atom_are_parsed_the_same_way():
    items = rss.parse_feed(RSS_XML, "https://acme-robotics.example/feed.xml")
    assert len(items) == 2
    assert items[0].title.startswith("Acme raises")
    assert items[0].url == "https://acme-robotics.example/news/series-b"
    assert items[0].published_at.startswith("2026-03-02")
    assert "growth investor" in items[0].summary

    atom = rss.parse_feed(ATOM_XML, "https://acme-robotics.example/atom.xml")
    assert len(atom) == 1
    assert atom[0].url == "https://acme-robotics.example/news/cfo"
    assert atom[0].published_at.startswith("2026-02-10")


def test_a_malformed_feed_yields_nothing_rather_than_raising():
    assert rss.parse_feed("<rss><channel><item>") == []
    assert rss.parse_feed("") == []
    assert rss.looks_like_feed("<html><body>not a feed") is False


def test_feed_discovery_prefers_the_declared_feed():
    candidates = rss.candidate_feed_urls(HOME, [f"{HOME}/custom/feed.xml"])
    assert candidates[0] == f"{HOME}/custom/feed.xml"
    assert f"{HOME}/feed" in candidates


def test_news_items_become_dated_sourced_signals(db):
    company = _seed_company()
    items = rss.parse_feed(RSS_XML, f"{HOME}/feed.xml")
    detected = signals.signals_from_news(company["id"], items)

    assert {s["signal_type"] for s in detected} == {"funding", "new_office"}
    for signal in detected:
        assert signal["occurred_at"]
        assert signal["source_url"].startswith(HOME)


# ---------------------------------------------------------------------------
# FR-344: the profile is shared, the job that built it is not
# ---------------------------------------------------------------------------


def test_a_refresh_job_is_only_visible_to_the_seeker_who_started_it(db):
    from dreamjob.api.deps import CurrentSeeker, current_seeker
    from dreamjob.api.routers import companies as router_module
    from dreamjob.jobs.runner import runner
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    def seeker(email: str) -> str:
        return insert_row(
            "job_seeker",
            {
                "email": email,
                "display_name": email.split("@")[0],
                "created_at": utcnow(),
                "updated_at": utcnow(),
            },
        )

    mine, theirs = seeker("mine@example.com"), seeker("theirs@example.com")
    their_job = runner.create("profiling", job_seeker_id=theirs)
    my_job = runner.create("profiling", job_seeker_id=mine)

    app = FastAPI()
    app.include_router(router_module.router, prefix="/api/companies")
    app.dependency_overrides[current_seeker] = lambda: CurrentSeeker(
        id=mine, email="mine@example.com", display_name="M", is_admin=False, locale="en"
    )
    with TestClient(app) as client:
        assert client.get(f"/api/companies/refresh-jobs/{my_job}").status_code == 200
        assert client.get(f"/api/companies/refresh-jobs/{their_job}").status_code == 404

