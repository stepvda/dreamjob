"""Every collection outcome is labelled for what it is (FR-182, FR-185, FR-186).

One campaign reported **538 collection errors**.  308 of them were ATS boards
answering 404 - dead slugs in a registry whose liveness was measured at 27.5%
for Wayback-only entries - 192 were robots.txt refusals the product made on
purpose, and 51 were bot walls.  Almost none was a failure, and a real failure
among five hundred expected outcomes is a failure nobody sees.

The fix is *not* silence.  The bug before this one was the opposite: a plan
item that fetched nothing, was blocked or crashed was written down as ``done``
with 0 records and 0 errors, and that single fact hid a total retrieval failure
for hours.  So every test here pins a label rather than a count of zero:

* a decision (robots.txt, a bot wall) is ``blocked`` and is *not* an error;
* a dead board is ``gone``, and the registry is told so it stops offering it;
* a 5xx, a transport error, a rate limit and a crash are still ``failed`` and
  still increment ``error_count`` - that is the regression that would quietly
  re-hide a real outage;
* the FR-186 budget is ``capped`` and an empty answer is still told from an
  answer nobody asked for.

Nothing here touches the network: ``EgressClient.fetch`` is replaced by a
canned answer and the adapters are stubs in the normal registry.
"""

from __future__ import annotations

import asyncio

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
from dreamjob.adapters.vacancy_source import SourceUnavailable
from dreamjob.config import get_settings
from dreamjob.db.connection import insert_row, query_all, query_one, upsert_row, utcnow
from dreamjob.db.migrator import migrate
from dreamjob.db.repositories import campaigns as campaign_repo
from dreamjob.egress.client import EgressClient, FetchResult, RobotsDisallowed
from dreamjob.jobs.runner import JobContext, runner
from dreamjob.pipeline import board_registry as registry
from dreamjob.pipeline import collection

VENDOR = "outcomevendor"


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
# Fixtures
# ---------------------------------------------------------------------------


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


@pytest.fixture()
def answers(monkeypatch):
    """Make every fetch answer one status, or raise robots.txt."""

    def _install(status: int | None, *, robots: bool = False):
        async def _fetch(self, url, **kwargs):
            if robots:
                raise RobotsDisallowed(f"robots.txt disallows {url}")
            return _answer(url, status or 200)

        monkeypatch.setattr(EgressClient, "fetch", _fetch)

    return _install


def _seeker() -> str:
    return insert_row(
        "job_seeker",
        {"email": "states@example.com", "display_name": "States",
         "created_at": utcnow(), "updated_at": utcnow()},
    )


def _campaign(seeker_id: str, **overrides) -> str:
    directive_id = insert_row(
        "directive_set",
        {"job_seeker_id": seeker_id, "name": "States", "created_at": utcnow(),
         "job_content": {"target_titles": ["Data Engineer"]},
         "location": {"countries": ["BE"]}},
    )
    profile_id = insert_row(
        "profile_version",
        {"job_seeker_id": seeker_id, "version": 1, "sections": {}, "created_at": utcnow()},
    )
    return campaign_repo.create_campaign(
        seeker_id,
        {"name": "States", "directive_set_id": directive_id, "profile_version_id": profile_id,
         "caps": {"max_pages": 20, "max_pages_per_source": 2}, **overrides},
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


def _run(campaign_id: str, seeker: str, checkpoint: dict | None = None) -> str:
    job_id = runner.create(collection.JOB_KIND, campaign_id=campaign_id, job_seeker_id=seeker)
    ctx = JobContext(job_id=job_id, campaign_id=campaign_id, job_seeker_id=seeker,
                     checkpoint=checkpoint or {})
    asyncio.run(collection.collection_worker(ctx))
    return job_id


def _items(campaign_id: str) -> dict[str, dict]:
    return {i["adapter_key"]: i for i in campaign_repo.list_plan_items(campaign_id)}


def _outcome(item: dict) -> dict:
    return ((item.get("caps") or {}).get("outcome")) or {}


# ---------------------------------------------------------------------------
# Stub adapters.  Each one fetches through the plan item's own view of the
# egress client and reports what it got the way the real ones do (a refusal is
# ``SourceUnavailable``, not an empty list).
# ---------------------------------------------------------------------------


class _Base(SourceAdapter):
    capabilities = AdapterCapabilities(pagination=False)
    URL = "https://states.test/jobs"

    def plan(self, directives, composite_profile, caps):
        return []

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        result = await self.egress.fetch(self.URL)
        if not result.ok:
            raise SourceUnavailable(
                f"[{self.key}] 1 request(s), 0 answered; HTTP {result.status_code}"
            )
        return [RawRecord(url=self.URL, content="{}",
                          meta={"title": "Data Engineer", "company_name_raw": "States NV",
                                "country": "BE"})]

    def parse(self, raw: RawRecord) -> list[dict]:
        return [dict(raw.meta)]

    def normalise(self, parsed: dict, raw: RawRecord) -> NormalisedRecord | None:
        return NormalisedRecord(entity_type="vacancy", data=dict(parsed))


@register_adapter
class _Board(_Base):
    """An ordinary job board with its own endpoint."""

    key = "outcome.board"
    display_name = "Outcome Board"
    source_type = SourceType.JOB_BOARD


@register_adapter
class _Ats(_Base):
    """One ATS board: the only target a 404 can mean *gone* for."""

    key = "outcome.ats"
    display_name = "Outcome ATS"
    source_type = SourceType.ATS
    vendor = VENDOR

    @classmethod
    def has_slug(cls, item) -> bool:
        query = item.native_query if hasattr(item, "native_query") else (
            item.get("native_query") or {}
        )
        return bool((query or {}).get("slug"))

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        url = f"https://ats.test/{item.native_query['slug']}"
        result = await self.egress.fetch(url)
        if not result.ok:
            raise SourceUnavailable(
                f"[{self.key}] 1 request(s), 0 answered; HTTP {result.status_code}"
            )
        return [RawRecord(url=url, content="{}",
                          meta={"title": "Platform Engineer", "country": "BE"})]


@register_adapter
class _Silent(_Base):
    """An adapter whose query it cannot use: it issues no request at all."""

    key = "outcome.silent"
    display_name = "Outcome Silent"
    source_type = SourceType.JOB_BOARD

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        return []


@register_adapter
class _Empty(_Base):
    """An adapter that fetches a page successfully and extracts nothing."""

    key = "outcome.empty"
    display_name = "Outcome Empty"
    source_type = SourceType.JOB_BOARD

    def parse(self, raw: RawRecord) -> list[dict]:
        return []


@register_adapter
class _EmptyStated(_Base):
    """A machine-readable board that answered 200 with an empty list.

    The payload is well-formed; it simply names no opening.  That is the
    employer having no open roles, not a changed layout.
    """

    key = "outcome.empty_stated"
    display_name = "Outcome Empty Stated"
    source_type = SourceType.ATS
    empty_parse_is_stated = True

    def parse(self, raw: RawRecord) -> list[dict]:
        return []


@register_adapter
class _Crash(_Base):
    """An adapter that raises after a good answer: our defect, and it stays loud."""

    key = "outcome.crash"
    display_name = "Outcome Crash"
    source_type = SourceType.JOB_BOARD

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        await self.egress.fetch(self.URL)
        raise ValueError("layout changed")


# ---------------------------------------------------------------------------
# blocked: we declined, correctly (FR-182, IR-101, CR-402)
# ---------------------------------------------------------------------------


def test_a_robots_refusal_is_blocked_and_is_not_an_error(db, answers):
    """192 of one campaign's 538 "errors" were the product working as designed."""
    answers(None, robots=True)
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("outcome.board")
    _plan_item(campaign_id, "outcome.board", {"queries": ["data"]}, pages=2)

    _run(campaign_id, seeker)

    item = _items(campaign_id)["outcome.board"]
    assert item["status"] == "blocked", "a refusal we made on purpose is not a failure"
    assert item["outcome_state"] == "blocked"
    assert item["error_count"] == 0, "the FR-185 error count is for things that went wrong"
    assert item["blocked_count"] == 1 and item["gone_count"] == 0
    assert item["failed_count"] == 0
    # NFR-402: the decision carries the evidence it rests on.
    assert "robots.txt" in (item["outcome_reason"] or "")
    assert "declined on principle" in (item["last_error"] or "")
    assert _outcome(item)["state"] == "blocked"
    assert _outcome(item)["blocked_by_robots"] is True
    # It is not paged through: robots.txt does not change its mind on page two.
    assert _outcome(item)["requests"] == 1
    # FR-185: the run's own error counter is not moved by a decision either.
    job = query_one("SELECT error_count FROM job_run WHERE kind = 'collection'")
    assert job["error_count"] == 0
    finished = query_one(
        "SELECT detail FROM audit_event WHERE action = 'campaign.collection_finished'"
    )
    assert '"blocked": 1' in finished["detail"] and '"errors": 0' in finished["detail"]


def test_a_bot_wall_is_blocked_and_is_not_an_error(db, answers):
    """51 HTTP 403s: the same decision as robots.txt, spelled in HTTP."""
    answers(403)
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("outcome.board")
    _plan_item(campaign_id, "outcome.board", {"queries": ["data"]})

    _run(campaign_id, seeker)

    item = _items(campaign_id)["outcome.board"]
    assert item["status"] == "blocked"
    assert item["outcome_state"] == "blocked"
    assert item["error_count"] == 0
    assert item["blocked_count"] == 1
    assert "HTTP 403" in (item["outcome_reason"] or "")
    assert _outcome(item)["blocked"] == 1
    assert _outcome(item)["blocked_by_robots"] is False, "a bot wall is not robots.txt"
    assert _outcome(item)["refused"] == 1, "it is still a refused request"


# ---------------------------------------------------------------------------
# gone: the target is not there any more (FR-343, DR-101)
# ---------------------------------------------------------------------------


def test_a_dead_board_is_gone_and_the_registry_is_told(db, answers):
    """308 of the 538 were dead slugs in a harvested registry (board_registry, N3)."""
    answers(404)
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("outcome.ats", source_type="ats")
    registry.mark_live(VENDOR, "acme", http_status=200)
    _plan_item(campaign_id, "outcome.ats", {"slug": "acme"})

    _run(campaign_id, seeker)

    item = _items(campaign_id)["outcome.ats"]
    assert item["status"] == "gone", "a board that has closed is a fact, not a failure"
    assert item["outcome_state"] == "gone"
    assert item["error_count"] == 0
    assert item["gone_count"] == 1 and item["blocked_count"] == 0
    assert "HTTP 404" in (item["outcome_reason"] or "")
    assert _outcome(item)["gone"] == 1

    # The registry learns, so a later campaign stops paying a request for it.
    # How many 404s retire a slug is the registry's policy, not collection's.
    assert registry.state_of(VENDOR, "acme") in registry.STATES
    board = registry.boards(vendor=VENDOR)[0]
    assert board["last_status"] == 404
    assert board["consecutive_failures"] == 1
    assert board["last_verified"], "this installation asked, and wrote down the answer"

    # NFR-403: a dead board is not an extraction attempt.  Feeding it to the
    # breakage detector made 96 dead Personio boards read as a broken adapter.
    assert not query_all(
        "SELECT id FROM audit_event WHERE action = 'adapter.breakage_suspected'"
    )


def test_a_dead_board_is_recorded_however_the_board_was_found(db, answers):
    """A board a crawl found is as worth remembering as one the registry shipped."""
    answers(404)
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("outcome.ats", source_type="ats")
    _plan_item(campaign_id, "outcome.ats", {"slug": "never-registered"})

    _run(campaign_id, seeker)

    assert _items(campaign_id)["outcome.ats"]["status"] == "gone"
    board = registry.boards(vendor=VENDOR)[0]
    assert (board["slug"], board["last_status"]) == ("never-registered", 404)


def test_a_404_on_a_source_that_is_not_a_board_is_still_a_failure(db, answers):
    """A search endpoint that answers 404 has moved: the adapter is wrong now."""
    answers(404)
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("outcome.board")
    _plan_item(campaign_id, "outcome.board", {"queries": ["data"]})

    _run(campaign_id, seeker)

    item = _items(campaign_id)["outcome.board"]
    assert item["status"] == "failed", "only a board the registry offered can be 'gone'"
    assert item["outcome_state"] == "failed"
    assert item["error_count"] == 1 and item["gone_count"] == 0
    assert item["failed_count"] == 1


# ---------------------------------------------------------------------------
# failed: the one the operator must see (FR-185)
# ---------------------------------------------------------------------------


def test_a_server_error_is_still_a_failure(db, answers):
    """The regression that would quietly re-hide a real outage.

    A 5xx is the shape of an outage - the source is there and it is broken -
    and reclassifying it as an expected outcome is exactly how the "done, 0
    records, 0 errors" bug came back the last time.  So it stays loud: on the
    plan item, on the job, and in the campaign's own status.
    """
    answers(503)
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("outcome.board")
    _plan_item(campaign_id, "outcome.board", {"queries": ["data"]})

    _run(campaign_id, seeker)

    item = _items(campaign_id)["outcome.board"]
    assert item["status"] == "failed", "a 5xx is not a decision and not a dead target"
    assert item["outcome_state"] == "failed"
    assert item["error_count"] == 1, "the operator's number still counts it"
    assert item["failed_count"] == 1
    assert item["blocked_count"] == 0 and item["gone_count"] == 0
    assert "HTTP 503" in (item["last_error"] or "")
    assert _outcome(item)["state"] == "failed"
    assert _outcome(item)["failed"] == 1
    job = query_one("SELECT error_count FROM job_run WHERE kind = 'collection'")
    assert job["error_count"] == 1, "the job reports it too"
    assert campaign_repo.get_campaign(campaign_id, seeker)["status"] == "failed"


def test_a_server_error_on_a_registry_board_is_a_failure_not_a_dead_board(db, answers):
    """A board that answers 500 is not gone: nothing may retire it."""
    answers(500)
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("outcome.ats", source_type="ats")
    registry.mark_live(VENDOR, "acme", http_status=200)
    _plan_item(campaign_id, "outcome.ats", {"slug": "acme"})

    _run(campaign_id, seeker)

    item = _items(campaign_id)["outcome.ats"]
    assert item["status"] == "failed"
    assert item["error_count"] == 1 and item["gone_count"] == 0
    board = registry.boards(vendor=VENDOR)[0]
    assert registry.state_of(VENDOR, "acme") == "live", (
        "an outage must never retire a live board"
    )
    assert board["consecutive_failures"] == 0
    assert board["last_status"] == 500, "the failure is visible in the register"



def test_an_adapter_that_crashes_is_a_failure(db, answers):
    """A parse crash is a defect of ours and stays in the error count."""
    answers(200)
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("outcome.crash")
    _plan_item(campaign_id, "outcome.crash", {"queries": ["data"]})

    _run(campaign_id, seeker)

    item = _items(campaign_id)["outcome.crash"]
    assert item["status"] == "failed"
    assert item["outcome_state"] == "failed"
    assert item["error_count"] == 1 and item["failed_count"] == 1
    assert "ValueError" in (item["last_error"] or "")


def test_a_rate_limit_is_a_failure_and_not_a_decision(db, answers):
    """429 is a rate this product is exceeding: the operator's problem to see."""
    answers(429)
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("outcome.board")
    _plan_item(campaign_id, "outcome.board", {"queries": ["data"]})

    _run(campaign_id, seeker)

    item = _items(campaign_id)["outcome.board"]
    assert item["status"] == "failed"
    assert item["error_count"] == 1 and item["blocked_count"] == 0


# ---------------------------------------------------------------------------
# succeeded, skipped, capped - and the audit that must survive (FR-186)
# ---------------------------------------------------------------------------


def test_a_source_that_wrote_records_succeeded(db, answers):
    answers(200)
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("outcome.board")
    _plan_item(campaign_id, "outcome.board", {"queries": ["data"]})

    _run(campaign_id, seeker)

    item = _items(campaign_id)["outcome.board"]
    assert item["status"] == "done"
    assert item["outcome_state"] == "succeeded"
    assert item["records_collected"] == 1
    assert item["error_count"] == 0 and item["blocked_count"] == 0 and item["gone_count"] == 0
    assert item["last_error"] is None


def test_fetching_nothing_is_still_told_from_finding_nothing(db, answers):
    """The audit won by the previous fix, which this one must not undo.

    ``succeeded-with-nothing`` and ``fetched-nothing`` are different answers and
    stay different rows: collapsing them back into one bucket is what hid a
    total retrieval failure behind ``done, 0 records, 0 errors``.
    """
    answers(200)
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("outcome.silent")
    _catalogue("outcome.empty")
    _plan_item(campaign_id, "outcome.silent", {"queries": ["data"]})
    _plan_item(campaign_id, "outcome.empty", {"queries": ["data"]})

    _run(campaign_id, seeker)

    items = _items(campaign_id)
    silent = items["outcome.silent"]
    assert silent["status"] == "skipped", "it never asked anything"
    assert silent["outcome_state"] == "no_work"
    assert silent["error_count"] == 1, "the silent failure is still counted"
    assert silent["failed_count"] == 1
    assert _outcome(silent)["requests"] == 0

    empty = items["outcome.empty"]
    assert empty["status"] == "failed", "it fetched a page and extracted nothing"
    assert empty["outcome_state"] == "extracted_nothing"
    assert empty["error_count"] == 1
    assert _outcome(empty)["requests"] == 1
    assert silent["outcome_state"] != empty["outcome_state"], (
        "two different answers, two different labels"
    )


def test_an_empty_machine_readable_board_is_no_matches_not_breakage(db, answers):
    """231 ATS rows read as breakage for boards whose only problem was no roles.

    They answered 200 with a well-formed empty list (``{"jobs": []}``, an RSS
    channel with no ``<item>``).  That is the source stating emptiness, so it is
    ``no_matches`` - and it must not be charged as an extraction attempt, which
    is what dragged four working adapters below NFR-403's 0.5 threshold.
    """
    answers(200)
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("outcome.empty_stated")
    _plan_item(campaign_id, "outcome.empty_stated", {"slug": "emptyco"})

    _run(campaign_id, seeker)

    item = _items(campaign_id)["outcome.empty_stated"]
    assert item["status"] == "done"
    assert item["outcome_state"] == "no_matches"
    assert item["error_count"] == 0
    assert _outcome(item)["stated_empty"] == 1
    assert _outcome(item)["extraction_rate"] is None, "not an extraction failure"


def test_an_empty_unstructured_board_is_still_breakage(db, answers):
    """The fix is opt-in: a scrape that parses to nothing is still NFR-403 data."""
    answers(200)
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("outcome.empty")
    _plan_item(campaign_id, "outcome.empty", {"queries": ["data"]})

    _run(campaign_id, seeker)

    item = _items(campaign_id)["outcome.empty"]
    assert item["outcome_state"] == "extracted_nothing"
    assert item["error_count"] == 1


def test_the_page_budget_is_capped_and_is_not_an_error(db, answers):
    """FR-186: 4,664 items of one campaign ended here and all were called errors."""
    answers(200)
    seeker = _seeker()
    campaign_id = _campaign(seeker, caps={"max_pages": 1, "max_pages_per_source": 1})
    _catalogue("outcome.board")
    _plan_item(campaign_id, "outcome.board", {"queries": ["first"]}, pages=5)
    _plan_item(campaign_id, "outcome.board", {"queries": ["second"]}, pages=5)

    _run(campaign_id, seeker)

    items = campaign_repo.list_plan_items(campaign_id)
    capped = [i for i in items if i["status"] == "planned"]
    assert capped, "the budget ran out before the second source"
    for item in capped:
        assert item["outcome_state"] == "capped"
        assert item["error_count"] == 0 and item["failed_count"] == 0
        assert "max_pages" in (item["outcome_reason"] or "")


def test_an_unrunnable_source_is_skipped_and_is_not_an_error(db, answers):
    """104 plan items named adapters that exist only as test fixtures."""
    answers(200)
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("outcome.deleted")
    # Written straight into the table: a plan outlives the code that ran it, so
    # a stored item may name an adapter that has since been deleted.
    insert_row(
        "source_plan_item",
        {"campaign_id": campaign_id, "adapter_key": "outcome.deleted",
         "native_query": '{"queries": ["data"]}', "estimated_pages": 1,
         "status": "planned", "created_at": utcnow()},
    )

    _run(campaign_id, seeker)

    item = _items(campaign_id)["outcome.deleted"]
    assert item["status"] == "skipped"
    assert item["outcome_state"] == "skipped"
    assert item["error_count"] == 0 and item["failed_count"] == 0


# ---------------------------------------------------------------------------
# The counts survive an interruption, and reach the dashboard (NFR-401, FR-185)
# ---------------------------------------------------------------------------


def test_the_checkpoint_carries_the_new_counts(db):
    """NFR-401: a resumed job reports the same totals as an uninterrupted one."""
    stats = collection.CollectionStats()
    stats.records, stats.errors, stats.blocked, stats.gone = 7, 2, 5, 3
    stats.adapter("outcome.board")["blocked"] += 5

    resumed = collection.CollectionStats()
    resumed.restore(stats.to_dict())

    assert resumed.records == 7 and resumed.errors == 2
    assert resumed.blocked == 5 and resumed.gone == 3
    assert resumed.by_adapter["outcome.board"]["blocked"] == 5
    # A checkpoint written before these counters existed must still resume.
    old = collection.CollectionStats()
    old.restore({"pages": 4, "records": 1, "by_adapter": {"outcome.board": {"errors": 1}}})
    assert old.records == 1 and old.blocked == 0
    assert old.adapter("outcome.board")["blocked"] == 0


def test_a_resumed_run_reports_the_same_totals(db, answers):
    answers(403)
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("outcome.board")
    _plan_item(campaign_id, "outcome.board", {"queries": ["data"]})
    saved = {"blocked": 4, "gone": 2, "errors": 1, "records": 3, "pages": 0}

    _run(campaign_id, seeker, checkpoint={"stats": saved})

    finished = query_one(
        "SELECT detail FROM audit_event WHERE action = 'campaign.collection_finished'"
    )
    assert '"blocked": 5' in finished["detail"], "the interrupted run's refusals still count"
    assert '"gone": 2' in finished["detail"]
    assert '"errors": 1' in finished["detail"], "and so do its failures"


def test_the_dashboard_reports_the_states_apart_from_the_failures(db, answers):
    """FR-185: "we declined 192 sources" is information, not 192 errors."""
    answers(403)
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("outcome.board")
    _plan_item(campaign_id, "outcome.board", {"queries": ["data"]})

    _run(campaign_id, seeker)
    status = collection.status(campaign_id, seeker)

    assert status["outcome_states"] == {"blocked": 1}
    source = status["sources"][0]
    assert source["outcome_state"] == "blocked"
    assert source["blocked_count"] == 1
    assert source["requests_blocked"] == 1
    assert source["error_count"] == 0
    assert status["progress"]["estimated_seconds_remaining"] == 0, (
        "a source we declined is finished, not pending"
    )


# ---------------------------------------------------------------------------
# The classification itself, without a campaign around it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "board", "expected"),
    [
        (403, False, "blocked"),
        (451, False, "blocked"),
        (404, True, "gone"),
        (410, True, "gone"),
        (404, False, "failed"),
        (401, False, "failed"),
        (429, False, "failed"),
        (500, True, "failed"),
        (503, True, "failed"),
    ],
)
def test_every_refusal_is_classified_exactly_once(status, board, expected):
    refusal = collection.Refusal(url="https://states.test/x", status=status)
    assert refusal.kind(board=board) == expected


def test_a_robots_refusal_needs_no_status_to_be_a_decision():
    refusal = collection.Refusal(url="https://states.test/x", status=None, robots=True)
    assert refusal.kind(board=True) == "blocked"
    assert "robots.txt" in refusal.detail


def test_a_failure_is_never_swallowed_by_an_expected_outcome():
    """Order of precedence: one real failure outranks any number of decisions."""
    outcome = collection.ItemOutcome(refused=9, blocked=8, gone=0, errors=1)
    assert outcome.state() == "failed"
    assert outcome.failed_requests == 1, "the buckets always add up to the refusals"

    declined = collection.ItemOutcome(refused=3, blocked=3)
    assert declined.state() == "blocked" and declined.failed_requests == 0

    dead = collection.ItemOutcome(refused=1, gone=1)
    assert dead.state() == "gone"

    collected = collection.ItemOutcome(created=1, refused=1, blocked=1)
    assert collected.state() == "succeeded", "records outrank a refusal on the same item"


# ---------------------------------------------------------------------------
# Migration 130
# ---------------------------------------------------------------------------


def test_migration_130_adds_the_outcome_columns(db):
    columns = {
        r["name"]: r
        for r in query_all("SELECT name, \"notnull\", dflt_value FROM pragma_table_info("
                           "'source_plan_item')")
    }
    for name in ("outcome_state", "outcome_reason", "blocked_count", "gone_count",
                 "failed_count"):
        assert name in columns, f"migration 130 did not add {name}"
    for name in ("blocked_count", "gone_count", "failed_count"):
        assert columns[name]["notnull"] == 1 and columns[name]["dflt_value"] == "0"
    # FR-185: error_count is narrowed, never dropped.
    assert "error_count" in columns
    indexes = {r["name"] for r in query_all(
        "SELECT name FROM sqlite_master WHERE type = 'index'"
    )}
    assert "idx_plan_outcome" in indexes


def test_a_board_that_answers_clears_the_strike_against_it(db, answers):
    """A rename in flight must not retire a board months later (FR-343).

    Two 404s retire a slug.  If nothing ever told the registry that a board had
    answered, the first 404 would sit on the row for ever and an unrelated one
    in a later campaign would retire a board that has been serving vacancies
    all along.
    """
    answers(200)
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("outcome.ats", source_type="ats")
    registry.mark_gone(VENDOR, "acme", 404)  # one strike, from an earlier run

    _plan_item(campaign_id, "outcome.ats", {"slug": "acme"})
    _run(campaign_id, seeker)

    assert _items(campaign_id)["outcome.ats"]["status"] == "done"
    assert registry.state_of(VENDOR, "acme") == "live"
    board = registry.boards(vendor=VENDOR)[0]
    assert board["consecutive_failures"] == 0, "the board answered; the strike is spent"
    assert board["job_count"] == 1


def test_a_blocked_board_says_nothing_about_whether_it_exists(db, answers):
    """robots.txt tells us what we may read, never what is there (FR-182)."""
    answers(None, robots=True)
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("outcome.ats", source_type="ats")
    registry.mark_live(VENDOR, "acme", http_status=200)

    _plan_item(campaign_id, "outcome.ats", {"slug": "acme"})
    _run(campaign_id, seeker)

    assert _items(campaign_id)["outcome.ats"]["status"] == "blocked"
    assert registry.state_of(VENDOR, "acme") == "live", (
        "a refusal to read is not evidence the board has gone"
    )
    assert registry.boards(vendor=VENDOR)[0]["consecutive_failures"] == 0


def test_a_broken_adapter_is_named_once_not_once_per_plan_item(db, answers):
    """NFR-403 belongs to the adapter, not to each plan item that used it.

    ``extraction_success_rate`` is read from the one ``source_catalogue`` row,
    so appending a breakage entry per item repeats the same fact once per item.
    Measured on the installed database: one campaign's status payload carried
    561 entries naming 6 adapters - ``board.eures (8%)`` 228 times - and the
    banner that renders them ran for a full screen and buried the failure list
    this screen exists to put first.
    """
    answers(200)
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("outcome.board", extraction_success_rate=0.08)
    for _ in range(5):
        _plan_item(campaign_id, "outcome.board", {"queries": ["data"]})

    status = collection.status(campaign_id, seeker)

    assert len(status["sources"]) == 5, "five items, so five source rows"
    assert status["adapter_breakage"] == [
        {"adapter_key": "outcome.board", "extraction_success_rate": 0.08}
    ], "one broken adapter is one entry, however many items named it"
