"""Collection runs several rate-limit buckets at once, and counts honestly.

Every test here pins a defect from ``docs/Data_Gathering_Plan.md`` sections 2.4,
3 and 5.2 (items N5 and N7):

* the page loop was strictly serial and grouped by adapter, so a campaign's
  wall-clock was the *sum* of its per-domain waits - 5.5 h for a campaign
  NFR-103 gives four hours;
* per-tenant ATS vendors have one host per customer, so nothing kept the
  product from opening ten connections to one vendor (Recruitee answered 429 to
  11 of 56 such requests);
* requests were attributed to a plan item by diffing the egress client's global
  counters, which is only true while exactly one item runs at a time;
* a board that answered 404 produced an empty record list, and an empty record
  list was written down as ``done, 0 records, 0 errors``;
* a board's postings named their employer but never created one, so
  ``max_companies`` measured nothing and 38 companies carried ``source = NULL``
  in breach of FR-166;
* nothing ever removed a stored body, so a weekly refresh orphaned ~8 GB a year.

Nothing here touches the network: ``EgressClient.fetch`` is replaced by a
canned answer and the adapters are stubs in the normal registry.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from dreamjob.adapters.base import (
    AdapterCapabilities,
    NormalisedRecord,
    PlanItem,
    RawRecord,
    SourceAdapter,
    SourceType,
    register_adapter,
)
from dreamjob.config import get_settings
from dreamjob.db.connection import insert_row, query_all, query_one, upsert_row, utcnow
from dreamjob.db.migrator import migrate
from dreamjob.db.repositories import campaigns as campaign_repo
from dreamjob.db.repositories import knowledge as kb_repo
from dreamjob.egress.client import EgressClient, FetchResult
from dreamjob.jobs.runner import JobContext, runner
from dreamjob.pipeline import collection


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DREAMJOB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DREAMJOB_DB_PATH", str(tmp_path / "data" / "test.db"))
    get_settings.cache_clear()
    get_settings()
    migrate()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def no_network(monkeypatch):
    """Every fetch answers 200 with an empty body, without leaving the process."""

    async def _fetch(self, url, **kwargs):
        return _answer(url, 200)

    monkeypatch.setattr(EgressClient, "fetch", _fetch)


def _answer(url: str, status: int) -> FetchResult:
    return FetchResult(
        url=url,
        status_code=status,
        text="{}",
        content=b"{}",
        headers={},
        from_cache=False,
        raw_document_id=None,
        content_hash="",
    )


# ---------------------------------------------------------------------------
# Fixtures: a job seeker, a campaign, a catalogue row, a run
# ---------------------------------------------------------------------------


def _seeker() -> str:
    return insert_row(
        "job_seeker",
        {"email": "buckets@example.com", "display_name": "Buckets",
         "created_at": utcnow(), "updated_at": utcnow()},
    )


def _campaign(seeker_id: str, **overrides) -> str:
    directive_id = insert_row(
        "directive_set",
        {"job_seeker_id": seeker_id, "name": "Buckets", "created_at": utcnow(),
         "job_content": {"target_titles": ["Data Engineer"]},
         "location": {"countries": ["BE"]}},
    )
    profile_id = insert_row(
        "profile_version",
        {"job_seeker_id": seeker_id, "version": 1, "sections": {}, "created_at": utcnow()},
    )
    return campaign_repo.create_campaign(
        seeker_id,
        {"name": "Buckets", "directive_set_id": directive_id, "profile_version_id": profile_id,
         "caps": {"max_pages": 50, "max_pages_per_source": 3}, **overrides},
    )


def _catalogue(adapter_key: str, **overrides) -> None:
    row = {
        "adapter_key": adapter_key,
        "display_name": adapter_key,
        "source_type": "job_board",
        "coverage_countries": ["BE"],
        "coverage_industries": [],
        "query_capabilities": AdapterCapabilities(pagination=False).__dict__,
        "access_method": "api",
        "rate_limit_rps": 0.5,
        "cost_per_call_eur": 0.0,
        "tos_status": "permitted",
        "enabled": 1,
        "requires_ack": 0,
        "updated_at": utcnow(),
    }
    row.update(overrides)
    upsert_row("source_catalogue", row, ["adapter_key"])


def _plan_item(campaign_id: str, adapter_key: str, query: dict, pages: int = 1) -> str:
    return campaign_repo.insert_plan_item(
        campaign_id,
        {"adapter_key": adapter_key, "native_query": query, "estimated_pages": pages,
         "created_at": utcnow()},
    )


def _run(campaign_id: str, seeker: str) -> str:
    job_id = runner.create(collection.JOB_KIND, campaign_id=campaign_id, job_seeker_id=seeker)
    ctx = JobContext(job_id=job_id, campaign_id=campaign_id, job_seeker_id=seeker, checkpoint={})
    asyncio.run(collection.collection_worker(ctx))
    return job_id


def _items(campaign_id: str) -> dict[str, dict]:
    return {i["adapter_key"]: i for i in campaign_repo.list_plan_items(campaign_id)}


def _outcome(item: dict) -> dict:
    return ((item.get("caps") or {}).get("outcome")) or {}


# ---------------------------------------------------------------------------
# Stub adapters
# ---------------------------------------------------------------------------


class _Base(SourceAdapter):
    capabilities = AdapterCapabilities(pagination=False)

    def plan(self, directives, composite_profile, caps):
        return []

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        return []

    def parse(self, raw: RawRecord) -> list[dict]:
        return [dict(raw.meta)]

    def normalise(self, parsed: dict, raw: RawRecord) -> NormalisedRecord | None:
        return NormalisedRecord(entity_type="vacancy", data=dict(parsed))


class _Gate:
    """Two adapters that only finish if they are running at the same time."""

    alpha: asyncio.Event
    beta: asyncio.Event

    @classmethod
    def reset(cls) -> None:
        cls.alpha = asyncio.Event()
        cls.beta = asyncio.Event()


@register_adapter
class _Alpha(_Base):
    key = "par.alpha"
    display_name = "Alpha"
    source_type = SourceType.JOB_BOARD
    API = "https://alpha.test/jobs"
    requests = 3

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        _Gate.alpha.set()
        await asyncio.wait_for(_Gate.beta.wait(), 5.0)
        for n in range(type(self).requests):
            await self.egress.fetch(f"{self.API}?p={n}")
        return [RawRecord(url=self.API, content="{}", meta={"title": "Alpha Engineer"})]


@register_adapter
class _Beta(_Base):
    key = "par.beta"
    display_name = "Beta"
    source_type = SourceType.JOB_BOARD
    API = "https://beta.test/jobs"
    requests = 1

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        _Gate.beta.set()
        await asyncio.wait_for(_Gate.alpha.wait(), 5.0)
        for n in range(type(self).requests):
            await self.egress.fetch(f"{self.API}?p={n}")
        return [RawRecord(url=self.API, content="{}", meta={"title": "Beta Engineer"})]


@register_adapter
class _Tenant(_Base):
    """A per-tenant ATS: one host per customer, one rate limit for the vendor."""

    key = "par.tenant"
    display_name = "Tenant ATS"
    source_type = SourceType.ATS
    vendor = "recruitee"
    capabilities = AdapterCapabilities(pagination=False, company_lookup=True)
    API = "https://{slug}.recruitee.com/api/offers/"
    live = 0
    peak = 0

    @classmethod
    def slugs_of(cls, item) -> list[str]:
        query = item.native_query if hasattr(item, "native_query") else (
            item.get("native_query") or {}
        )
        slug = str((query or {}).get("slug") or "")
        return [slug] if slug else []

    @classmethod
    def has_slug(cls, item) -> bool:
        return bool(cls.slugs_of(item))

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        cls = type(self)
        cls.live += 1
        cls.peak = max(cls.peak, cls.live)
        try:
            slug = self.slugs_of(item)[0]
            for _ in range(3):
                await asyncio.sleep(0)  # any real request yields to the loop
            await self.egress.fetch(self.API.format(slug=slug))
            # Two postings whose employer is spelled two ways: one board, one
            # company, however inconsistently the board names its owner.
            return [
                RawRecord(url=self.API.format(slug=slug), content="{}",
                          meta={"title": title, "company_name_raw": name})
                for title, name in (
                    (f"Engineer at {slug}", f"{slug} NV"),
                    (f"Analyst at {slug}", f"{slug.upper()} N.V."),
                )
            ]
        finally:
            cls.live -= 1


@register_adapter
class _DeadBoard(_Base):
    """A company-lookup source whose endpoint answers 404 and says nothing."""

    key = "par.dead"
    display_name = "Dead Board"
    source_type = SourceType.DIRECTORY
    capabilities = AdapterCapabilities(pagination=False, company_lookup=True)
    API = "https://dead.test/api/companies"

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        result = await self.egress.fetch(self.API)
        return [] if not result.ok else [RawRecord(url=self.API, content="{}", meta={})]


@register_adapter
class _Aggregator(_Base):
    """An aggregator in the shape of EURES: an employer name, no board."""

    key = "par.aggregator"
    display_name = "Aggregator"
    source_type = SourceType.JOB_BOARD
    API = "https://europa.test/eures"

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        await self.egress.fetch(self.API)
        return [
            RawRecord(url=self.API, content="{}",
                      meta={"title": title, "company_name_raw": "Zephyr Logistics NV",
                            "country": "BE"})
            for title in ("Data Engineer", "Data Analyst")
        ]


@register_adapter
class _HalfDead(_Base):
    """A source one of whose two boards is gone: it still returns the other."""

    key = "par.half"
    display_name = "Half Dead"
    source_type = SourceType.JOB_BOARD
    API = "https://half.test/boards"

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        records = []
        for name in ("gone", "live"):
            result = await self.egress.fetch(f"{self.API}/{name}")
            if result.ok:
                records.append(
                    RawRecord(url=result.url, content="{}", meta={"title": f"Engineer ({name})"})
                )
        return records


@pytest.fixture(autouse=True)
def _reset_stubs():
    _Gate.reset()
    _Tenant.live = 0
    _Tenant.peak = 0
    yield


# ---------------------------------------------------------------------------
# N5: buckets (FR-182, NFR-103)
# ---------------------------------------------------------------------------


def test_a_per_tenant_vendor_is_one_bucket_however_many_subdomains_it_has():
    """A vendor sees one client IP whatever the tenant host is (plan section 2.2)."""
    item = {"adapter_key": "ats.recruitee", "native_query": {"slug": "acme"}}
    assert collection._bucket_for(item, _Tenant()) == "*.recruitee.com"

    class _Personio(_Tenant):
        vendor = "personio"

    class _Teamtailor(_Tenant):
        vendor = "teamtailor"

    class _Workday(_Tenant):
        vendor = "workday"

    assert collection._bucket_for(item, _Personio()) == "*.jobs.personio.de"
    assert collection._bucket_for(item, _Teamtailor()) == "*.teamtailor.com"
    assert collection._bucket_for(item, _Workday()) == "*.myworkdayjobs.com"

    # A shared-host vendor is bucketed by its host, so two boards of it are
    # sequential against that host and parallel to everything else.
    assert collection._bucket_for({"adapter_key": "par.alpha"}, _Alpha()) == "alpha.test"
    # A plan item that names its own URL is bucketed by that URL's host.
    crawl = {"adapter_key": "website.crawler", "native_query": {"url": "https://acme.be/jobs"}}
    assert collection._bucket_for(crawl, _Base()) == "acme.be"
    # And a source whose host cannot be known stays as serial as it is today.
    assert collection._bucket_for({"adapter_key": "x.y"}, _Base()) == "adapter:x.y"


def test_sources_in_different_buckets_run_at_the_same_time(db, no_network):
    """NFR-103: the wall-clock is the longest bucket, not the sum of them.

    Each adapter waits for the other to start.  Under the serial loop this test
    cannot pass at all: the first source would wait five seconds for a source
    that only runs after it has finished.
    """
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    for key in ("par.alpha", "par.beta"):
        _catalogue(key)
        _plan_item(campaign_id, key, {"queries": ["data"]})

    _run(campaign_id, seeker)

    items = _items(campaign_id)
    assert items["par.alpha"]["status"] == "done", items["par.alpha"]["last_error"]
    assert items["par.beta"]["status"] == "done", items["par.beta"]["last_error"]

    # ... and each item was charged its own requests, not the run's.  The old
    # measure diffed the client's global counters, so with two sources in flight
    # Beta's single request was reported as four.
    assert _outcome(items["par.alpha"])["requests"] == 3
    assert _outcome(items["par.beta"])["requests"] == 1


def test_two_boards_of_one_vendor_never_run_at_the_same_time(db, no_network):
    """Recruitee answered 429 to 11 of 56 requests spread over 10 tenants."""
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("par.tenant", source_type="ats")
    for slug in ("alpha", "beta", "gamma"):
        _plan_item(campaign_id, "par.tenant", {"slug": slug})

    _run(campaign_id, seeker)

    assert _Tenant.peak == 1, f"{_Tenant.peak} boards of one vendor were read at once"
    assert len(query_all("SELECT id FROM vacancy")) == 6, "all three boards were read"


# ---------------------------------------------------------------------------
# N7: honest accounting (FR-185, NFR-403)
# ---------------------------------------------------------------------------


def test_a_source_whose_endpoint_answers_404_is_not_recorded_as_done(db, monkeypatch):
    """A refused request is a failure, not a source with nothing to offer."""

    async def _fetch(self, url, **kwargs):
        return _answer(url, 404)

    monkeypatch.setattr(EgressClient, "fetch", _fetch)
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("par.dead", source_type="directory")
    _plan_item(campaign_id, "par.dead", {"companies": ["acme"]})

    _run(campaign_id, seeker)

    item = _items(campaign_id)["par.dead"]
    assert item["status"] == "failed", "a 404 that returned no rows is not 'done'"
    assert item["error_count"] == 1
    assert "HTTP 404" in (item["last_error"] or "")
    assert _outcome(item)["refused"] == 1
    assert _outcome(item)["requests"] == 1
    assert campaign_repo.get_campaign(campaign_id, seeker)["status"] == "failed"


# ---------------------------------------------------------------------------
# Company rows (FR-166, FR-186, DR-101)
# ---------------------------------------------------------------------------


def test_a_board_creates_the_company_whose_board_it_is(db, no_network):
    """FR-186: ``max_companies`` cannot measure companies nothing creates."""
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("par.tenant", source_type="ats")
    _plan_item(campaign_id, "par.tenant", {"slug": "acme"})

    _run(campaign_id, seeker)

    companies = query_all("SELECT * FROM company")
    assert len(companies) == 1, "two spellings of one employer are one company"
    company = companies[0]
    assert (company["ats_vendor"], company["ats_slug"]) == ("recruitee", "acme"), (
        "the identity is the board, which is what the next campaign can read again"
    )
    # FR-166: 38 companies carried source = NULL because nothing recorded who
    # produced them.
    assert company["source"] == "par.tenant"
    provenance = kb_repo.provenance_for("company", company["id"])
    # The company-enrichment pass records its own provenance for the same
    # company (FR-341), so the board's row is no longer necessarily first. What
    # FR-166 requires is that the board that produced the company is recorded
    # and attributed to the plan item - not that it heads the list.
    board_rows = [p for p in provenance if p["adapter_key"] == "par.tenant"]
    assert board_rows, "the board that produced the company is recorded"
    assert board_rows[0]["source_plan_item_id"]

    vacancies = query_all("SELECT company_id FROM vacancy")
    assert len(vacancies) == 2
    assert {v["company_id"] for v in vacancies} == {company["id"]}

    # FR-185/FR-186: the run reports the company it collected for, which is
    # the number ``max_companies`` bounds and which used to be zero always.
    finished = query_one(
        "SELECT detail FROM audit_event WHERE action = 'campaign.collection_finished'"
    )
    assert '"companies": 1' in finished["detail"]


def test_a_board_whose_company_is_already_known_is_not_duplicated(db, no_network):
    """DR-101: the board identity resolves to the company that already runs it."""
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("par.tenant", source_type="ats")
    known = kb_repo.insert_company(
        {"name": "Acme Group", "normalised_name": "acme group", "country": "BE",
         "ats_vendor": "recruitee", "ats_slug": "acme", "source": "seed",
         "collected_at": utcnow()}
    )
    _plan_item(campaign_id, "par.tenant", {"slug": "acme"})

    _run(campaign_id, seeker)

    assert [c["id"] for c in query_all("SELECT id FROM company")] == [known]
    assert {v["company_id"] for v in query_all("SELECT company_id FROM vacancy")} == {known}


# ---------------------------------------------------------------------------
# Hygiene: indexes and the orphan sweep (FR-183, DR-102)
# ---------------------------------------------------------------------------


def test_migration_093_indexes_the_scans_collection_repeats_per_row(db):
    """Without these the unresolved-company scan costs 18 ms per new vacancy."""
    names = {
        r["name"]
        for r in query_all("SELECT name FROM sqlite_master WHERE type = 'index'")
    }
    assert "idx_vacancy_company_name" in names
    assert "idx_prov_type_created" in names
    assert "idx_company_ats_board" in names


def _document(name: str, *, days_old: int, **overrides) -> str:
    settings = get_settings()
    path = settings.raw_dir / f"{name}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("<html/>", encoding="utf-8")
    fetched = (datetime.now(UTC) - timedelta(days=days_old)).isoformat(timespec="seconds")
    values = {
        "url": f"https://example.test/{name}",
        "content_type": "text/html",
        "content_hash": name,
        "storage_path": str(path.relative_to(settings.abs_data_dir)),
        "byte_size": 7,
        "http_status": 200,
        "fetched_at": fetched,
    }
    values.update(overrides)
    return insert_row("raw_document", values)


def test_orphaned_raw_documents_are_pruned_after_the_retention_window(db):
    """~160 MB a week is orphaned by a weekly refresh and nothing evicted it."""
    settings = get_settings()
    orphan = _document("orphan", days_old=40)
    cited = _document("cited", days_old=40)
    recent = _document("recent", days_old=1)
    cached = _document("cached", days_old=40)
    kb_repo.record_provenance("vacancy", "v1", raw_document_id=cited, adapter_key="x")
    upsert_row(
        "http_cache",
        {"url_hash": "h", "url": "https://example.test/cached",
         "body_path": str(settings.raw_dir / "cached.html"), "status_code": 200,
         "fetched_at": utcnow(), "expires_at": "2099-01-01T00:00:00+00:00"},
        ["url_hash"],
    )

    assert collection.prune_raw_documents("campaign-1") == 1

    remaining = {r["id"] for r in query_all("SELECT id FROM raw_document")}
    assert orphan not in remaining, "nothing referred to it and it was 40 days old"
    assert remaining == {cited, recent, cached}
    assert not Path(settings.raw_dir / "orphan.html").exists(), "the body went with the row"
    assert Path(settings.raw_dir / "cached.html").exists(), (
        "a body a live cache entry still serves is not an orphan"
    )
    audit = query_one("SELECT detail FROM audit_event WHERE action = 'raw_document.pruned'")
    assert audit and '"documents": 1' in audit["detail"]


def test_a_refusal_is_still_counted_when_the_rest_of_the_page_worked(db, monkeypatch):
    """FR-185: a source that half answered is not a source that answered.

    The records it did collect are kept - dropping them would throw away
    requests already spent - but the 404 is counted against the plan item and
    named in its last error, instead of vanishing because something else on the
    same page succeeded.
    """

    async def _fetch(self, url, **kwargs):
        return _answer(url, 404 if url.endswith("/gone") else 200)

    monkeypatch.setattr(EgressClient, "fetch", _fetch)
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("par.half")
    _plan_item(campaign_id, "par.half", {"queries": ["data"]})

    _run(campaign_id, seeker)

    item = _items(campaign_id)["par.half"]
    assert item["status"] == "done", "the board that answered was collected"
    assert item["records_collected"] == 1
    assert item["error_count"] == 1, "the board that did not answer was counted"
    assert "HTTP 404" in (item["last_error"] or "")
    assert _outcome(item)["refused"] == 1
    assert _outcome(item)["state"] == "succeeded"


def test_an_aggregators_employer_becomes_a_company_with_a_source(db, no_network):
    """FR-166: 38 companies carried ``source = NULL`` because nothing said who
    produced them.  An employer named on an advert is still a company, and the
    row it creates records the adapter and the plan item that found it."""
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("par.aggregator")
    _plan_item(campaign_id, "par.aggregator", {"queries": ["data"]})

    _run(campaign_id, seeker)

    companies = query_all("SELECT * FROM company")
    assert len(companies) == 1
    assert companies[0]["name"] == "Zephyr Logistics NV"
    assert companies[0]["source"] == "par.aggregator", "no company row may be sourceless"
    assert {v["company_id"] for v in query_all("SELECT company_id FROM vacancy")} == {
        companies[0]["id"]
    }
    assert kb_repo.provenance_for("company", companies[0]["id"])
