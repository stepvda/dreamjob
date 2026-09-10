"""The seams between the nine data-gathering slices, pinned where they parted.

Every slice's own suite was green and the product still retrieved nothing,
because each defect here lives *between* two files that no single slice owned:

* the planner refused to partition EURES unless the adapter declared it read the
  partition keys, and the adapter never declared them - so a "partitioned"
  Campaign B was one country-wide plan item instead of 133 (N1 <-> N2);
* the planner wrote the sector axis as ``nace_section`` and the adapter read
  ``sector_codes``, so even a forced partition sent the same body for all
  eighteen NACE sections of a region - 40 plan items, 3 distinct requests;
* a partition the register genuinely has nothing for was reported as
  ``failed`` with "the source layout has probably changed", which at sweep scale
  buries the real breakages among false ones (N7 over-correcting);
* the fetch ledger table and its writer both shipped, and nothing ever called
  it, so per-target reuse had no evidence to work from (N4).

Nothing here touches the network.
"""

from __future__ import annotations

import asyncio

import pytest
from dreamjob.adapters.base import (
    AdapterCapabilities,
    NormalisedRecord,
    PlanItem,
    RawRecord,
    SourceType,
    register_adapter,
)
from dreamjob.adapters.jobboards.eures import EuresAdapter
from dreamjob.adapters.vacancy_source import VacancySourceAdapter
from dreamjob.config import get_settings
from dreamjob.db.connection import insert_row, query_all, upsert_row, utcnow
from dreamjob.db.migrator import migrate
from dreamjob.db.repositories import campaigns as campaign_repo
from dreamjob.egress import client as egress_client
from dreamjob.egress.client import EgressClient, FetchResult
from dreamjob.jobs.runner import JobContext, runner
from dreamjob.pipeline import collection, discovery


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
# N1 <-> N2: the planner partitions only what the adapter can execute
# ---------------------------------------------------------------------------


def test_the_eures_adapter_declares_the_keys_the_planner_partitions_on():
    """Without this the planner degrades to one country-wide item, silently."""
    assert discovery.reads_partitions(EuresAdapter) is True


def test_belgium_last_week_is_one_plan_item_per_region_and_sector():
    items = discovery.eures_partitions(
        countries=["BE"], caps={}, partitioned=discovery.reads_partitions(EuresAdapter)
    )
    # 3 NUTS-1 regions x 19 NACE Rev 2.1 sections (only T, U and V are left
    # out, for yield).  N is professional services and is swept; O is where
    # staffing sits and is also swept - agencies are a per-employer tag, not a
    # partition the sweep refuses to look at.
    assert len(items) == 57


def test_belgium_and_the_netherlands_are_the_plans_own_133_partitions():
    items = discovery.eures_partitions(countries=["BE", "NL"], caps={}, partitioned=True)
    assert len(items) == 133


def _body(native: dict) -> str:
    return repr(sorted(
        EuresAdapter.search_body(
            "",
            EuresAdapter.locations_of(native),
            1,
            EuresAdapter.page_size(native),
            publication_period=EuresAdapter.publication_period(native),
            sector_codes=EuresAdapter.sector_codes(native),
        ).items()
    ))


def test_every_partition_asks_the_service_a_different_question():
    """The defect: 18 sections of a region all sent byte-for-byte one body."""
    items = discovery.eures_partitions(countries=["BE"], caps={}, partitioned=True)
    assert len({_body(i.native_query) for i in items}) == len(items) == 57


def test_the_sector_axis_survives_the_planners_spelling_of_it():
    """``nace_section`` is written by the planner and read as ``sectorCodes``."""
    assert EuresAdapter.sector_codes({"nace_section": "J"}) == ["J"]
    items = discovery.eures_partitions(countries=["BE"], caps={}, partitioned=True)
    sent = {tuple(EuresAdapter.search_body(
        "", ["BE1"], 1, 50, sector_codes=EuresAdapter.sector_codes(i.native_query),
    ).get("sectorCodes") or ()) for i in items}
    assert ("J",) in sent
    assert () not in sent, "a partition that names a sector must send one"


def test_a_partition_narrows_to_its_region_rather_than_its_country():
    items = discovery.eures_partitions(countries=["BE"], caps={}, partitioned=True)
    assert EuresAdapter.locations_of(items[0].native_query) == ["BE1"]


# ---------------------------------------------------------------------------
# N7: "the register has nothing here" is not "the parser broke"
# ---------------------------------------------------------------------------


def test_a_source_that_states_a_total_of_zero_is_read_not_broken():
    outcome = collection.ItemOutcome(requests=1, pages=1, stated_empty=1)
    assert outcome.state() == "no_matches"
    assert collection._STATE_STATUS[outcome.state()] == "done"
    # The noun follows the source: a board holds vacancies, a register holds
    # records, and the message says which rather than assuming the caller's.
    assert "no record for this query" in (collection._state_message(outcome) or "")
    assert "no vacancy for this query" in (
        collection._state_message(outcome, holds="vacancy") or ""
    )


def test_a_source_that_fetched_and_parsed_nothing_is_still_a_failure():
    """The N7 guarantee this must not weaken."""
    outcome = collection.ItemOutcome(requests=1, pages=1)
    assert outcome.state() == "extracted_nothing"
    assert collection._STATE_STATUS[outcome.state()] == "failed"


def test_the_eures_adapter_reads_the_total_the_service_states():
    assert EuresAdapter._stated_total({"numberRecords": 0, "jvs": []}) == 0
    assert EuresAdapter._stated_total({"numberRecords": 1593}) == 1593
    # No stated total at all is not a claim of emptiness.
    assert EuresAdapter._stated_total({"jvs": []}) is None
    assert EuresAdapter._stated_total("<html>") is None


# ---------------------------------------------------------------------------
# End to end: an empty partition, a full one, and the ledger they both write
# ---------------------------------------------------------------------------


class _Board(VacancySourceAdapter):
    """A source that states its own result count, the way EURES does."""

    key = "gather.board"
    display_name = "Stating board"
    source_type = SourceType.JOB_BOARD
    capabilities = AdapterCapabilities(pagination=False)
    API = "https://stating.test/search"
    #: number of rows the next fetch should claim and return
    rows = 2

    def plan(self, directives, composite_profile, caps):
        return []

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        # ``_get`` is the counted route: ``settle`` distinguishes "issued no
        # request" from "asked and was told nothing" by that count.
        result = await self._get(self.API)
        if not self.rows:
            # The source answered and said it holds nothing for this query.
            self.fetch_outcome.stated_empty += 1
            return self.settle([])
        return self.settle([
            RawRecord(url=result.url if result else self.API, content="{}",
                      content_type="application/json",
                      meta={"title": f"Job {n}", "company_name_raw": "Stating BV"})
            for n in range(self.rows)
        ])

    def parse(self, raw: RawRecord) -> list[dict]:
        return [dict(raw.meta)]

    def normalise(self, parsed: dict, raw: RawRecord) -> NormalisedRecord | None:
        return NormalisedRecord(entity_type="vacancy", data=dict(parsed))


try:  # the registry is process-wide; registering twice is not an error here
    register_adapter(_Board)
except Exception:  # noqa: BLE001 - already registered by an earlier import
    pass


@pytest.fixture()
def no_network(monkeypatch):
    async def _fetch(self, url, **kwargs):
        return FetchResult(url=url, status_code=200, text="{}", content=b"{}",
                           headers={"ETag": 'W/"abc"'}, from_cache=False,
                           raw_document_id=None, content_hash="deadbeef")

    monkeypatch.setattr(EgressClient, "fetch", _fetch)


def _campaign_with_item(query: dict) -> tuple[str, str]:
    seeker = insert_row("job_seeker", {
        "email": "gather@example.com", "display_name": "Gather",
        "created_at": utcnow(), "updated_at": utcnow()})
    directive_id = insert_row("directive_set", {
        "job_seeker_id": seeker, "name": "G", "created_at": utcnow(),
        "job_content": {}, "location": {"countries": ["BE"]}})
    profile_id = insert_row("profile_version", {
        "job_seeker_id": seeker, "version": 1, "sections": {}, "created_at": utcnow()})
    campaign_id = campaign_repo.create_campaign(seeker, {
        "name": "G", "directive_set_id": directive_id, "profile_version_id": profile_id,
        "caps": {"max_pages": 10, "max_pages_per_source": 1}})
    upsert_row("source_catalogue", {
        "adapter_key": _Board.key, "display_name": _Board.key, "source_type": "job_board",
        "coverage_countries": ["BE"], "coverage_industries": [],
        "query_capabilities": AdapterCapabilities(pagination=False).__dict__,
        "access_method": "api", "rate_limit_rps": 0.5, "cost_per_call_eur": 0.0,
        "tos_status": "permitted", "enabled": 1, "requires_ack": 0, "updated_at": utcnow(),
    }, ["adapter_key"])
    campaign_repo.insert_plan_item(campaign_id, {
        "adapter_key": _Board.key, "native_query": query, "estimated_pages": 1,
        "created_at": utcnow()})
    return campaign_id, seeker


def _run(campaign_id: str, seeker: str) -> None:
    job_id = runner.create(collection.JOB_KIND, campaign_id=campaign_id, job_seeker_id=seeker)
    asyncio.run(collection.collection_worker(
        JobContext(job_id=job_id, campaign_id=campaign_id, job_seeker_id=seeker, checkpoint={})))


def test_an_empty_partition_finishes_without_being_called_broken(db, no_network, monkeypatch):
    monkeypatch.setattr(_Board, "rows", 0)
    campaign_id, seeker = _campaign_with_item({"partition": "empty"})
    _run(campaign_id, seeker)
    item = campaign_repo.list_plan_items(campaign_id)[0]
    assert item["status"] == "done"
    assert item["error_count"] == 0, "a source with nothing to give owes no error"
    assert "layout" not in (item["last_error"] or "")
    assert "nothing to collect" in (item["last_error"] or "")


def test_a_partition_with_rows_still_collects_them(db, no_network, monkeypatch):
    monkeypatch.setattr(_Board, "rows", 2)
    campaign_id, seeker = _campaign_with_item({"partition": "full"})
    _run(campaign_id, seeker)
    item = campaign_repo.list_plan_items(campaign_id)[0]
    assert item["status"] == "done"
    assert item["records_collected"] == 2


def test_every_item_that_fetched_leaves_a_row_in_the_fetch_ledger(db, no_network, monkeypatch):
    """N4: the table and its writer shipped; nothing called it."""
    monkeypatch.setattr(_Board, "rows", 2)
    campaign_id, seeker = _campaign_with_item({"partition": "ledgered"})
    _run(campaign_id, seeker)
    rows = query_all("SELECT * FROM fetch_ledger WHERE adapter_key = ?", (_Board.key,))
    assert len(rows) == 1
    assert rows[0]["http_status"] == 200
    assert rows[0]["record_count"] == 2
    assert rows[0]["etag"] == 'W/"abc"'


def test_a_target_that_gave_nothing_is_still_remembered_as_read(db, no_network, monkeypatch):
    """A dead target must not be re-probed every run just because it was empty."""
    monkeypatch.setattr(_Board, "rows", 0)
    campaign_id, seeker = _campaign_with_item({"partition": "empty-but-read"})
    _run(campaign_id, seeker)
    rows = query_all("SELECT * FROM fetch_ledger WHERE adapter_key = ?", (_Board.key,))
    assert len(rows) == 1
    assert rows[0]["record_count"] == 0
    assert egress_client.last_fetch(_Board.key, rows[0]["target_key"]) is not None


def test_two_partitions_of_one_adapter_are_two_ledger_targets(db, no_network, monkeypatch):
    """Per-adapter freshness made one board stand for all of a vendor's boards."""
    monkeypatch.setattr(_Board, "rows", 1)
    campaign_id, seeker = _campaign_with_item({"partition": "one"})
    campaign_repo.insert_plan_item(campaign_id, {
        "adapter_key": _Board.key, "native_query": {"partition": "two"},
        "estimated_pages": 1, "created_at": utcnow()})
    _run(campaign_id, seeker)
    rows = query_all("SELECT * FROM fetch_ledger WHERE adapter_key = ?", (_Board.key,))
    assert len({r["target_key"] for r in rows}) == 2


# ---------------------------------------------------------------------------
# FR-182: a Crawl-delay a strict parser drops is still a Crawl-delay
# ---------------------------------------------------------------------------


def test_europa_is_paced_at_the_delay_it_publishes():
    """europa.eu asks for 10 s; the sweep ran at the product's own 2 s.

    Its robots.txt puts a blank line between ``User-agent: *`` and
    ``Crawl-delay: 10``.  A blank line ends a record, so ``urllib.robotparser``
    attributes the directive to nobody and ``crawl_delay()`` answers ``None`` -
    the parser is right and the pace was still wrong.
    """
    limiter = egress_client.DomainLimiter(0.5)   # our own rate: 2 s
    assert limiter.interval_for("europa.eu") == 10.0
    assert limiter.interval_for("www.europa.eu") == 10.0, "subdomains inherit it"
    assert limiter.interval_for("unrelated.example") == 2.0


def test_a_parsed_delay_still_wins_when_it_is_slower():
    limiter = egress_client.DomainLimiter(0.5)
    limiter.set_crawl_delay("europa.eu", 30)
    assert limiter.interval_for("europa.eu") == 30.0


def test_the_published_floor_never_speeds_a_domain_up():
    limiter = egress_client.DomainLimiter(0.01)   # our own rate: 100 s
    assert limiter.interval_for("europa.eu") == 100.0


def test_switching_crawl_delays_off_switches_the_floor_off_too():
    limiter = egress_client.DomainLimiter(0.5, honour_crawl_delay=False)
    assert limiter.interval_for("europa.eu") == 2.0
