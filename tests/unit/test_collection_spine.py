"""The collection spine tells success from silence (FR-161..166, FR-181..186).

Every test here pins a defect that used to be invisible: a source that fetched
nothing, was refused, or was handed a query it could not read was written down
as ``status='done', records_collected=0, error_count=0`` - the same row a
source with genuinely no matching vacancies produces.  So the assertions are
mostly about *failure being visible*, not about the happy path.

Nothing here touches the network: the adapters are stubs registered in the
normal registry, and the one real payload is a response captured from a live
Greenhouse board (``tests/fixtures/ats/greenhouse_gitlab_jobs.json``).
"""

from __future__ import annotations

import asyncio
import json
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
from dreamjob.jobs.runner import JobCancelled, JobContext, runner
from dreamjob.pipeline import collection, planning

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ats"


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
# Fixtures: a job seeker with directives in the FR-142/FR-144 shape
# ---------------------------------------------------------------------------


def _seeker() -> str:
    return insert_row(
        "job_seeker",
        {
            "email": "spine@example.com",
            "display_name": "Spine",
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )


def _campaign(seeker_id: str, **overrides) -> str:
    directives = {
        "job_content": {"target_titles": ["Data Engineer"], "titles": ["Data Engineer"]},
        # The FR-144 shape as the directive editor writes it: the real place
        # labels sit in `areas`, next to fields that are not places at all.
        "location": {
            "areas": [
                {
                    "label": "Brussels, Belgium",
                    "country_code": "BE",
                    "place_type": "city",
                    "radius_km": 25.0,
                    "geocoded_at": "2026-09-08T22:07:27+00:00",
                }
            ],
            "countries": ["BE"],
            "commute_mode": "car",
        },
    }
    directives.update(overrides.pop("directives", {}))
    directive_id = insert_row(
        "directive_set",
        {"job_seeker_id": seeker_id, "name": "Spine", "created_at": utcnow(), **directives},
    )
    profile_id = insert_row(
        "profile_version",
        {"job_seeker_id": seeker_id, "version": 1, "sections": {}, "created_at": utcnow()},
    )
    return campaign_repo.create_campaign(
        seeker_id,
        {
            "name": "Spine",
            "directive_set_id": directive_id,
            "profile_version_id": profile_id,
            "caps": {"max_pages": 20, "max_pages_per_source": 3},
            **overrides,
        },
    )


def _catalogue(adapter_key: str, **overrides) -> None:
    row = {
        "adapter_key": adapter_key,
        "display_name": adapter_key.title(),
        "source_type": "job_board",
        "coverage_countries": ["BE"],
        "coverage_industries": [],
        "query_capabilities": AdapterCapabilities().__dict__,
        "access_method": "http",
        "rate_limit_rps": 1.0,
        "cost_per_call_eur": 0.0,
        "tos_status": "permitted",
        "enabled": 1,
        "requires_ack": 0,
        "updated_at": utcnow(),
    }
    row.update(overrides)
    upsert_row("source_catalogue", row, ["adapter_key"])


def _run(campaign_id: str, seeker: str, checkpoint: dict | None = None) -> str:
    job_id = runner.create(
        collection.JOB_KIND, campaign_id=campaign_id, job_seeker_id=seeker
    )
    ctx = JobContext(
        job_id=job_id,
        campaign_id=campaign_id,
        job_seeker_id=seeker,
        checkpoint=checkpoint or {},
    )
    asyncio.run(collection.collection_worker(ctx))
    return job_id


def _by_key(campaign_id: str) -> dict[str, dict]:
    return {i["adapter_key"]: i for i in campaign_repo.list_plan_items(campaign_id)}


# ---------------------------------------------------------------------------
# Stub adapters
# ---------------------------------------------------------------------------


class _Base(SourceAdapter):
    """Shared shape: records what fetch() was handed, so a query can be checked."""

    seen: list[dict] = []

    def plan(self, directives: dict, composite_profile: dict, caps: dict) -> list[PlanItem]:
        return []

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        type(self).seen.append(dict(item.native_query))
        return []

    def parse(self, raw: RawRecord) -> list[dict]:
        return []

    def normalise(self, parsed: dict, raw: RawRecord) -> NormalisedRecord | None:
        return NormalisedRecord(entity_type="vacancy", data=dict(parsed))


@register_adapter
class _SpineBoard(_Base):
    """A board that plans itself in its own vocabulary and returns vacancies."""

    key = "spine.board"
    display_name = "Spine Board"
    source_type = SourceType.JOB_BOARD
    seen: list[dict] = []

    def plan(self, directives: dict, composite_profile: dict, caps: dict) -> list[PlanItem]:
        job_content = directives.get("job_content") or {}
        location = directives.get("location") or {}
        return [
            PlanItem(
                adapter_key=self.key,
                native_query={
                    "queries": list(job_content.get("titles") or []),
                    "locations": list(location.get("cities") or []),
                    "pages": 2,
                },
                estimated_pages=2,
                rationale="keyword search over the public listing",
            )
        ]

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        type(self).seen.append(dict(item.native_query))
        page = int(item.native_query.get("page") or 1)
        return [RawRecord(url=f"https://spine.test/jobs?p={page}", content="<html/>",
                          meta={"page": page})]

    def parse(self, raw: RawRecord) -> list[dict]:
        page = raw.meta["page"]
        return [
            {"title": f"Data Engineer {page}", "company_name_raw": "Spine NV",
             "location": "Brussels", "country": "BE", "posted_at": "2026-09-01"}
        ]


@register_adapter
class _SilentBoard(_Base):
    """A board whose query it cannot use: it issues no request at all."""

    key = "spine.silent"
    display_name = "Silent Board"
    source_type = SourceType.JOB_BOARD
    seen: list[dict] = []


@register_adapter
class _EmptyExtractor(_Base):
    """A board that fetches a page successfully and extracts nothing from it."""

    key = "spine.empty"
    display_name = "Empty Extractor"
    source_type = SourceType.JOB_BOARD
    seen: list[dict] = []

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        type(self).seen.append(dict(item.native_query))
        return [RawRecord(url="https://spine.test/listing", content="<html>changed</html>")]


@register_adapter
class _RefusedRecords(_Base):
    """A board whose every record the knowledge base refuses (no title)."""

    key = "spine.refused"
    display_name = "Refused Records"
    source_type = SourceType.JOB_BOARD
    seen: list[dict] = []

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        return [RawRecord(url="https://spine.test/listing", content="<html/>")]

    def parse(self, raw: RawRecord) -> list[dict]:
        return [{"company_name_raw": "Spine NV"}]  # FR-184: a vacancy needs a title


@register_adapter
class _Discovery(_Base):
    """A crawl that finds a company and the ATS board it runs (FR-222)."""

    key = "spine.discovery"
    display_name = "Discovery Crawl"
    source_type = SourceType.WEBSITE
    capabilities = AdapterCapabilities(pagination=False, company_lookup=True)
    seen: list[dict] = []

    def plan(self, directives: dict, composite_profile: dict, caps: dict) -> list[PlanItem]:
        return [PlanItem(adapter_key=self.key, native_query={"url": "https://spine.test"})]

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        type(self).seen.append(dict(item.native_query))
        return [RawRecord(url="https://spine.test", content="<html/>")]

    def parse(self, raw: RawRecord) -> list[dict]:
        return [{"name": "Boardful NV", "domain": "boardful.test",
                 "ats_vendor": "spinevendor", "ats_slug": "boardful"}]

    def normalise(self, parsed: dict, raw: RawRecord) -> NormalisedRecord:
        return NormalisedRecord(entity_type="company", data=dict(parsed))


@register_adapter
class _SpineAts(_Base):
    """An ATS board, planned one item per company that runs this vendor."""

    key = "spine.ats"
    display_name = "Spine ATS"
    source_type = SourceType.ATS
    vendor = "spinevendor"
    capabilities = AdapterCapabilities(pagination=False, company_lookup=True)
    seen: list[dict] = []

    @classmethod
    def has_slug(cls, item) -> bool:
        query = item.native_query if hasattr(item, "native_query") else (
            item.get("native_query") or {}
        )
        return bool((query or {}).get("slug"))

    def plan(self, directives: dict, composite_profile: dict, caps: dict) -> list[PlanItem]:
        return [
            PlanItem(
                adapter_key=self.key,
                native_query={"slug": c["ats_slug"], "company_name": c.get("name")},
                estimated_pages=1,
            )
            for c in caps.get("companies") or []
            if str(c.get("ats_vendor") or "").lower() == self.vendor and c.get("ats_slug")
        ]

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        type(self).seen.append(dict(item.native_query))
        return [RawRecord(url=f"https://ats.test/{item.native_query['slug']}", content="{}",
                          meta={"slug": item.native_query["slug"]})]

    def parse(self, raw: RawRecord) -> list[dict]:
        return [{"title": "Platform Engineer", "company_name_raw": raw.meta["slug"],
                 "location": "Brussels", "country": "BE"}]


@register_adapter
class _HostileSkip(_Base):
    """An ATS adapter whose skip decision raises - the recorded live failure."""

    key = "spine.hostile"
    display_name = "Hostile Skip"
    source_type = SourceType.ATS
    vendor = ""
    seen: list[dict] = []

    @classmethod
    def has_slug(cls, item) -> bool:
        raise KeyError(0)  # exactly what slug_of() raised on a mapping board_slugs


@pytest.fixture(autouse=True)
def _reset_stubs():
    for cls in (_SpineBoard, _SilentBoard, _EmptyExtractor, _RefusedRecords,
                _Discovery, _SpineAts, _HostileSkip):
        cls.seen = []
    yield


# ---------------------------------------------------------------------------
# Planning (FR-162, FR-164, FR-186)
# ---------------------------------------------------------------------------


def test_a_plan_item_speaks_the_adapters_own_query_vocabulary(db):
    """FR-162: the adapter authors its query, so its fetch() can read it."""
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("spine.board")

    summary = planning.generate_plan(campaign_id, seeker, use_llm=False)

    query = summary["items"][0]["native_query"]
    assert query["queries"] == ["Data Engineer"], "the planner's keywords reached the adapter's key"
    assert query["locations"] == ["Brussels, Belgium"]
    # The keys the planner used to invent, which no adapter ever read.
    assert "keywords" not in query or query.get("queries")

    _run(campaign_id, seeker)
    assert _SpineBoard.seen, "the source was actually asked for something"
    assert _SpineBoard.seen[0]["queries"] == ["Data Engineer"]
    assert _SpineBoard.seen[0]["locations"] == ["Brussels, Belgium"]


def test_the_deterministic_query_names_a_place_and_not_the_commute_mode(db):
    """FR-144: 'car' is how the seeker travels, not a town to search in."""
    location = {
        "areas": [{"label": "Brussels, Belgium", "country_code": "BE", "place_type": "city",
                   "geocoded_at": "2026-09-08T22:07:27+00:00"}],
        "countries": ["BE"],
        "commute_mode": "car",
        "max_commute_minutes": 45,
    }
    places = planning._locations({"location": location})
    assert "car" not in places and "city" not in places
    assert places[0] == "Brussels, Belgium"
    assert planning.target_countries({"location": location}) == ["BE"]

    # An area with no label at all still must not fall back to the commute mode.
    empty = {"areas": [], "countries": ["BE"], "commute_mode": "car", "place_type": "city"}
    assert "car" not in planning._locations({"location": empty})

    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("no_adapter_here")  # nothing registered: the deterministic path
    summary = planning.generate_plan(campaign_id, seeker, use_llm=False)
    query = summary["items"][0]["native_query"]
    assert query["location"] != "car"
    assert query["locations"] == ["Brussels, Belgium"]
    assert query["queries"] and query["country_codes"] == ["BE"]


def test_a_company_scoped_source_with_no_company_is_rejected_with_a_reason(db):
    """FR-164: an ATS source with no board to read is not planned as an empty query."""
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("spine.ats", source_type="ats")

    summary = planning.generate_plan(campaign_id, seeker, use_llm=False)

    assert summary["items"] == [], "a source with nothing to read is not planned"
    reasons = {r["adapter_key"]: r["reason"] for r in summary["rejected_sources"]}
    assert "spine.ats" in reasons
    assert "discovery" in reasons["spine.ats"], reasons["spine.ats"]
    assert "board" in reasons["spine.ats"]


def test_an_ats_source_is_planned_once_per_company_and_replanning_is_stable(db):
    """FR-162/FR-166: one unit of work per board, and its row survives a re-plan."""
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("spine.ats", source_type="ats")
    for name, slug in (("Alpha NV", "alpha"), ("Beta BV", "beta")):
        kb_repo.insert_company(
            {"name": name, "normalised_name": name.lower(), "country": "BE",
             "ats_vendor": "spinevendor", "ats_slug": slug, "collected_at": utcnow()}
        )

    first = planning.generate_plan(campaign_id, seeker, use_llm=False)
    slugs = sorted((i["native_query"] or {}).get("slug") for i in first["items"])
    assert slugs == ["alpha", "beta"], "one plan item per company, not one per vendor"

    # FR-166: a collected record points at its plan item, and a re-plan of the
    # same work must not hand that row to a different company.
    alpha = next(i for i in first["items"] if i["native_query"]["slug"] == "alpha")
    knowledge_writer = collection.knowledge_base.KnowledgeBaseWriter(
        adapter_key="spine.ats", plan_item_id=alpha["id"], campaign_id=campaign_id
    )
    knowledge_writer.write(
        {"entity_type": "vacancy", "data": {"title": "Platform Engineer",
                                            "company_name_raw": "Alpha NV"}}
    )
    again = planning.generate_plan(campaign_id, seeker, use_llm=False)
    same = next(i for i in again["items"] if i["native_query"]["slug"] == "alpha")
    assert same["id"] == alpha["id"], "the row the record points at still describes alpha"
    assert campaign_repo.collected_counts(campaign_id)["vacancy"] == 1


def test_a_plan_never_asks_for_more_pages_than_the_campaign_budget(db):
    """FR-186: the plan is fitted to max_pages, and no source is deleted by it."""
    seeker = _seeker()
    campaign_id = _campaign(seeker, caps={"max_pages": 4, "max_pages_per_source": 10})
    for key in ("board_a", "board_b", "board_c", "board_d"):
        _catalogue(key)

    summary = planning.generate_plan(campaign_id, seeker, use_llm=False)

    pages = [i["estimated_pages"] for i in summary["items"]]
    assert len(pages) == 4
    assert all(p >= 1 for p in pages), "every source keeps a floor of one page"
    assert sum(pages) <= 4, f"the plan overruns its own cap: {pages}"
    assert summary["totals"]["exceeds_max_pages"] == 0
    assert summary["page_budget"]["requested"] > summary["page_budget"]["granted"]


def test_industry_directives_narrow_source_selection(db):
    """FR-142/FR-164: an industry the source does not cover is a rejection."""
    seeker = _seeker()
    campaign_id = _campaign(
        seeker,
        directives={"job_content": {"target_titles": ["Data Engineer"],
                                    "industries_include": ["biotech"]}},
    )
    _catalogue("board_bio", coverage_industries=["biotech"])
    _catalogue("board_fin", coverage_industries=["finance"])

    summary = planning.generate_plan(campaign_id, seeker, use_llm=False)

    assert {i["adapter_key"] for i in summary["items"]} == {"board_bio"}
    reasons = {r["adapter_key"]: r["reason"] for r in summary["rejected_sources"]}
    assert "industry" in reasons["board_fin"]


class _CountingLLM:
    """Answers with a well-formed but empty plan list, and counts the calls."""

    def __init__(self, campaign_id: str):
        self.campaign_id = campaign_id
        self.calls: list[bool | None] = []

    class budget:  # noqa: N801 - mirrors LLMClient.budget
        @staticmethod
        def should_degrade() -> bool:
            return False

    def complete_json(self, task, *, system, user, untrusted=None, **kw):
        self.calls.append(kw.get("prefer_strong"))
        return {"plans": []}


def test_an_empty_but_valid_model_answer_does_not_escalate_to_the_strong_model(db):
    """The reasoning model is the fallback for a failed call, not for a quiet one."""
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("spine.board")

    llm = _CountingLLM(campaign_id)
    summary = planning.generate_plan(campaign_id, seeker, use_llm=True, llm=llm)

    assert llm.calls == [False], "one cheap call; the strong model was not asked again"
    assert summary["items"], "the adapter's own plan still stands"


def test_plan_items_run_discovery_before_harvest(db):
    """FR-181: the sources that find companies run before the ones that need one."""
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("spine.board")
    _catalogue("spine.discovery", source_type="website")
    _catalogue("spine.ats", source_type="ats")
    kb_repo.insert_company(
        {"name": "Alpha NV", "normalised_name": "alpha nv", "country": "BE",
         "ats_vendor": "spinevendor", "ats_slug": "alpha", "collected_at": utcnow()}
    )
    planning.generate_plan(campaign_id, seeker, use_llm=False)

    order = [i["adapter_key"] for i in campaign_repo.list_plan_items(campaign_id)]
    assert order.index("spine.board") < order.index("spine.discovery") < order.index("spine.ats")


# ---------------------------------------------------------------------------
# Collection outcomes (FR-181, FR-185, NFR-403)
# ---------------------------------------------------------------------------


def test_a_source_that_issues_no_request_is_not_recorded_as_done(db):
    """The 'done, 0 records, 0 errors' row is the failure this whole fix is about."""
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("spine.silent")
    campaign_repo.insert_plan_item(
        campaign_id,
        {"adapter_key": "spine.silent", "native_query": {"keywords": ["data"]},
         "estimated_pages": 3, "created_at": utcnow()},
    )

    _run(campaign_id, seeker)

    item = _by_key(campaign_id)["spine.silent"]
    assert item["status"] != "done", "a source that fetched nothing did not do its job"
    assert item["status"] == "skipped"
    assert item["error_count"] == 1, "the silent failure is counted"
    assert "no request" in item["last_error"]
    assert (item["caps"] or {})["outcome"]["state"] == "no_work"
    assert (item["caps"] or {})["outcome"]["requests"] == 0
    assert (item["caps"] or {})["outcome"]["charged_pages"] == 0, (
        "a page on which nothing was fetched does not spend the run's budget"
    )
    # FR-186: a page on which nothing was fetched is not charged to the budget,
    # and the source is not asked for its remaining two pages either.
    assert len(_SilentBoard.seen) == 1
    # FR-185: a run that retrieved nothing at all is not a completed run.
    assert campaign_repo.get_campaign(campaign_id, seeker)["status"] == "failed"
    audit = query_one(
        "SELECT detail FROM audit_event WHERE action = 'campaign.collection_finished'"
    )
    assert '"outcome": "failed"' in audit["detail"]


def test_a_source_that_fetched_and_extracted_nothing_is_flagged_as_broken(db):
    """NFR-403: 200s that parse to nothing is exactly the breakage signal."""
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("spine.empty")
    campaign_repo.insert_plan_item(
        campaign_id,
        {"adapter_key": "spine.empty", "native_query": {"queries": ["data"]},
         "estimated_pages": 1, "created_at": utcnow()},
    )

    _run(campaign_id, seeker)

    item = _by_key(campaign_id)["spine.empty"]
    assert item["status"] == "failed"
    assert item["error_count"] == 1
    assert "extracted no record" in item["last_error"]
    assert (item["caps"] or {})["outcome"]["state"] == "extracted_nothing"
    assert (item["caps"] or {})["outcome"]["charged_pages"] == 1

    # The rolling rate is recorded even though no raw record was ever parsed -
    # the old measure could only see adapters that were already working.
    entry = query_one(
        "SELECT extraction_success_rate FROM source_catalogue WHERE adapter_key = ?",
        ("spine.empty",),
    )
    assert entry["extraction_success_rate"] == 0.0
    audits = query_all(
        "SELECT entity_id FROM audit_event WHERE action = 'adapter.breakage_suspected'"
    )
    assert [a["entity_id"] for a in audits] == ["spine.empty"], "flagged on the first page"


def test_records_the_knowledge_base_refuses_are_counted_as_errors(db):
    """A source whose every record was dropped is not a source that found nothing."""
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("spine.refused")
    campaign_repo.insert_plan_item(
        campaign_id,
        {"adapter_key": "spine.refused", "native_query": {"queries": ["data"]},
         "estimated_pages": 1, "created_at": utcnow()},
    )

    _run(campaign_id, seeker)

    item = _by_key(campaign_id)["spine.refused"]
    assert item["status"] == "failed"
    assert item["error_count"] == 1
    assert "refused" in item["last_error"]
    assert (item["caps"] or {})["outcome"]["dropped"] == 1
    assert not query_all("SELECT id FROM vacancy")


def test_the_page_budget_reserves_a_floor_for_every_source(db):
    """FR-186: an exhausted budget shortens every source, it does not delete the tail."""
    seeker = _seeker()
    campaign_id = _campaign(seeker, caps={"max_pages": 3, "max_pages_per_source": 5})
    _catalogue("spine.board")
    for suffix in ("a", "b", "c"):
        campaign_repo.insert_plan_item(
            campaign_id,
            {"adapter_key": "spine.board", "native_query": {"queries": [suffix]},
             "estimated_pages": 5, "created_at": f"2026-09-0{1 + ord(suffix) - 97}T00:00:00+00:00"},
        )

    _run(campaign_id, seeker)

    asked = [q["queries"][0] for q in _SpineBoard.seen]
    assert sorted(set(asked)) == ["a", "b", "c"], (
        f"every source got a page of the budget, not just the first: {asked}"
    )
    items = campaign_repo.list_plan_items(campaign_id)
    assert all(i["records_collected"] >= 1 for i in items)


def test_a_cancelled_run_rewinds_the_item_it_interrupted(db):
    """NFR-401: an item interrupted mid-page is runnable again, not stuck 'running'."""
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("spine.board")
    item_id = campaign_repo.insert_plan_item(
        campaign_id,
        {"adapter_key": "spine.board", "native_query": {"queries": ["data"]},
         "estimated_pages": 3, "created_at": utcnow()},
    )

    job_id = runner.create(collection.JOB_KIND, campaign_id=campaign_id, job_seeker_id=seeker)
    ctx = JobContext(job_id=job_id, campaign_id=campaign_id, job_seeker_id=seeker)

    async def cancel_after_first_page() -> None:
        if _SpineBoard.seen:
            raise JobCancelled("cancelled by user")

    ctx.checkpoint_barrier = cancel_after_first_page  # type: ignore[method-assign]
    with pytest.raises(JobCancelled):
        asyncio.run(collection.collection_worker(ctx))

    item = campaign_repo.get_plan_item(item_id)
    assert item["status"] == "planned", "a cancelled item does not stay 'running' for ever"
    assert "cancelled" in (item["last_error"] or "")


def test_a_broken_skip_decision_fails_only_its_own_plan_item(db):
    """IR-101: one malformed plan item must not take the whole job down."""
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("spine.hostile", source_type="ats")
    _catalogue("spine.board")
    hostile = campaign_repo.insert_plan_item(
        campaign_id,
        {"adapter_key": "spine.hostile", "native_query": {"board_slugs": {"primary": "x"}},
         "estimated_pages": 1, "created_at": utcnow()},
    )
    healthy = campaign_repo.insert_plan_item(
        campaign_id,
        {"adapter_key": "spine.board", "native_query": {"queries": ["data"]},
         "estimated_pages": 1, "created_at": utcnow()},
    )

    _run(campaign_id, seeker)

    broken = campaign_repo.get_plan_item(hostile)
    assert broken["status"] == "failed"
    assert broken["error_count"] == 1
    assert "KeyError" in broken["last_error"], "the exception type is part of the diagnosis"
    good = campaign_repo.get_plan_item(healthy)
    assert good["status"] == "done" and good["records_collected"] == 1, (
        "the healthy source still ran"
    )


def test_a_source_that_succeeds_clears_its_previous_error(db):
    """FR-185: a stale failure does not outlive the run that fixed it."""
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("spine.board")
    item_id = campaign_repo.insert_plan_item(
        campaign_id,
        {"adapter_key": "spine.board", "native_query": {"queries": ["data"]},
         "estimated_pages": 1, "last_error": "stopped by cap: max_pages (FR-186)",
         "created_at": utcnow()},
    )

    _run(campaign_id, seeker)

    item = campaign_repo.get_plan_item(item_id)
    assert item["status"] == "done"
    assert item["last_error"] is None, "the previous run's cap message is gone"


def test_an_ats_board_discovered_during_the_run_is_read_in_the_same_run(db):
    """FR-181: discover the company, then read its board - in one pass."""
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("spine.discovery", source_type="website",
               query_capabilities=AdapterCapabilities(pagination=False).__dict__)
    _catalogue("spine.ats", source_type="ats",
               query_capabilities=AdapterCapabilities(pagination=False).__dict__)

    summary = planning.generate_plan(campaign_id, seeker, use_llm=False)
    assert [i["adapter_key"] for i in summary["items"]] == ["spine.discovery"], (
        "no company has a board yet, so the ATS source is not planned"
    )

    _run(campaign_id, seeker)

    assert _SpineAts.seen and _SpineAts.seen[0]["slug"] == "boardful", (
        "the board the crawl found was read in the same run"
    )
    harvested = _by_key(campaign_id)["spine.ats"]
    assert harvested["status"] == "done"
    assert harvested["records_collected"] == 1
    titles = [v["title"] for v in query_all("SELECT title FROM vacancy")]
    assert titles == ["Platform Engineer"]


def test_a_captured_greenhouse_board_lands_in_the_knowledge_base(db):
    """End to end on a real payload: plan -> fetch -> parse -> knowledge base.

    The response is the one captured from the live GitLab Greenhouse board, so
    this pins the whole spine against a shape the source really produces.
    """
    payload = json.loads((FIXTURES / "greenhouse_gitlab_jobs.json").read_text(encoding="utf-8"))

    @register_adapter
    class _CapturedGreenhouse(_Base):
        key = "spine.greenhouse"
        display_name = "Greenhouse (captured)"
        source_type = SourceType.ATS
        vendor = "greenhouse"
        capabilities = AdapterCapabilities(pagination=False, company_lookup=True)
        seen: list[dict] = []

        @classmethod
        def has_slug(cls, item) -> bool:
            query = item.native_query if hasattr(item, "native_query") else (
                item.get("native_query") or {}
            )
            return bool((query or {}).get("slug"))

        def plan(self, directives: dict, composite_profile: dict, caps: dict) -> list[PlanItem]:
            return [
                PlanItem(adapter_key=self.key, native_query={"slug": c["ats_slug"]},
                         estimated_pages=1)
                for c in caps.get("companies") or []
                if str(c.get("ats_vendor") or "").lower() == self.vendor and c.get("ats_slug")
            ]

        async def fetch(self, item: PlanItem) -> list[RawRecord]:
            type(self).seen.append(dict(item.native_query))
            return [RawRecord(url="https://boards-api.greenhouse.io/v1/boards/gitlab/jobs",
                              content=json.dumps(payload), content_type="application/json")]

        def parse(self, raw: RawRecord) -> list[dict]:
            return [
                {"title": job["title"], "company_name_raw": "GitLab",
                 "location": (job.get("location") or {}).get("name"),
                 "source_url": job.get("absolute_url")}
                for job in json.loads(raw.content).get("jobs", [])[:25]
            ]

    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("spine.greenhouse", source_type="ats",
               query_capabilities=AdapterCapabilities(pagination=False).__dict__)
    kb_repo.insert_company(
        {"name": "GitLab", "normalised_name": "gitlab", "domain": "gitlab.com", "country": "BE",
         "ats_vendor": "greenhouse", "ats_slug": "gitlab", "collected_at": utcnow()}
    )

    summary = planning.generate_plan(campaign_id, seeker, use_llm=False)
    assert summary["items"][0]["native_query"]["slug"] == "gitlab"

    _run(campaign_id, seeker)

    expected = len(payload["jobs"])
    item = _by_key(campaign_id)["spine.greenhouse"]
    assert item["status"] == "done"
    assert item["records_collected"] == expected, "every posting on the board was written"
    assert (item["caps"] or {})["outcome"]["state"] == "succeeded"
    vacancies = query_all("SELECT title, company_id FROM vacancy")
    assert len(vacancies) == expected
    assert all(v["company_id"] for v in vacancies), "every posting is linked to its employer"
    assert campaign_repo.collected_counts(campaign_id)["vacancy"] == len(vacancies)
    assert campaign_repo.get_campaign(campaign_id, seeker)["status"] == "completed"


def test_the_progress_total_counts_only_the_sources_that_can_run(db):
    """FR-185: a run that could only ever do one page does not report '1 of 8'."""
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("spine.board")
    _catalogue("spine.hostile", source_type="ats")
    campaign_repo.insert_plan_item(
        campaign_id,
        {"adapter_key": "spine.board", "native_query": {"queries": ["data"]},
         "estimated_pages": 2, "created_at": utcnow()},
    )
    campaign_repo.insert_plan_item(
        campaign_id,
        {"adapter_key": "no_adapter_here", "native_query": {"queries": ["data"]},
         "estimated_pages": 5, "created_at": utcnow()},
        # A stored plan outlives the code that ran it, so this state is real -
        # but nothing may *plan* a source with neither adapter nor catalogue
        # row (C5), so writing one takes the explicit keyword.
        allow_unimplemented=True,
    )

    job_id = _run(campaign_id, seeker)

    job = query_one("SELECT progress_done, progress_total FROM job_run WHERE id = ?", (job_id,))
    assert job["progress_total"] == 2, (
        "the denominator counts the pages of the sources that were actually runnable"
    )
    assert job["progress_done"] == 2, "and the run reached it"


def test_a_plan_written_before_stages_existed_still_runs_discovery_first(db):
    """FR-181: ordering does not depend on a migration having been run."""
    campaign_id = _campaign(_seeker())
    created = "2026-09-08T23:22:21+00:00"  # one plan, one second, as the planner writes it
    for key in ("ats.greenhouse", "board.vdab", "website.crawl", "news.rss"):
        campaign_repo.insert_plan_item(
            campaign_id, {"adapter_key": key, "native_query": {}, "created_at": created}
        )

    order = [i["adapter_key"] for i in campaign_repo.list_plan_items(campaign_id)]

    assert order == ["board.vdab", "news.rss", "website.crawl", "ats.greenhouse"], order
    assert campaign_repo.plan_item_stage({"adapter_key": "ats.greenhouse"}) == (
        campaign_repo.STAGE_HARVEST
    )
    assert campaign_repo.plan_item_stage(
        {"adapter_key": "board.vdab", "caps": {"stage": 3}}
    ) == campaign_repo.STAGE_HARVEST, "an explicit stage wins over the fall-back"
