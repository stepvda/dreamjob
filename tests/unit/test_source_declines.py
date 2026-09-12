"""A refusal that repeats is a fact about the source, not a per-page answer.

One autopilot run collected nothing because **Indeed answered 403 to all five
of its requests and Jobat to all ten**.  Every plan item was recorded
``blocked`` - correctly; a bot wall is a decision, not a defect - but the fact
that the *adapter* was unusable died with the campaign.  The next plan selected
the same five sources, the next run re-issued the same refused requests, the
next log repeated the same errors, and the operator had no list of what had
been declined or any way to say "try this one again".

These tests pin the memory and its boundaries (FR-182, IR-101, FR-186):

* three refusals in one run, or one refusal in each of two runs, declines the
  adapter and is recorded with reason, counts and evidence URL;
* a single 403 is still only a ``blocked`` page - the per-page behaviour is
  exactly what it was - and a source that collected anything, or that had a
  real failure (5xx, timeout), is never declined;
* the next plan excludes a declined adapter, and the collection worker skips
  its items without charging a page or incrementing ``error_count``;
* a terms-prohibited adapter is declined immediately and stays declined until
  an administrator explicitly acknowledges it;
* a plan with no runnable source fails with the names and reasons instead of
  launching an empty campaign;
* the autopilot page cap comes from ``planning`` in one place, and the
  scale-down the cap forces is stored on the campaign and surfaced to the plan
  screen and the run report.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

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
from dreamjob.db.repositories import admin as admin_repo
from dreamjob.db.repositories import campaigns as campaign_repo
from dreamjob.db.repositories import declines as decline_repo
from dreamjob.egress.client import EgressClient, FetchResult
from dreamjob.jobs.runner import JobContext, runner
from dreamjob.pipeline import autopilot, collection, declines, planning


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
# Fixtures and stubs
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
    def _install(status: int):
        async def _fetch(self, url, **kwargs):
            return _answer(url, status)

        monkeypatch.setattr(EgressClient, "fetch", _fetch)

    return _install


def _seeker(email: str = "declines@example.com") -> str:
    return insert_row(
        "job_seeker",
        {"email": email, "display_name": "Declines", "created_at": utcnow(),
         "updated_at": utcnow()},
    )


def _campaign(seeker_id: str, *, profile_version: int = 1, **overrides) -> str:
    directive_id = insert_row(
        "directive_set",
        {"job_seeker_id": seeker_id, "name": f"Declines {profile_version}",
         "created_at": utcnow(),
         "job_content": {"target_titles": ["Data Engineer"]},
         "location": {"countries": ["BE"]}},
    )
    profile_id = insert_row(
        "profile_version",
        {"job_seeker_id": seeker_id, "version": profile_version, "sections": {},
         "created_at": utcnow()},
    )
    return campaign_repo.create_campaign(
        seeker_id,
        {"name": "Declines", "directive_set_id": directive_id,
         "profile_version_id": profile_id,
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


def _run(campaign_id: str, seeker: str) -> str:
    job_id = runner.create(collection.JOB_KIND, campaign_id=campaign_id, job_seeker_id=seeker)
    ctx = JobContext(job_id=job_id, campaign_id=campaign_id, job_seeker_id=seeker)
    asyncio.run(collection.collection_worker(ctx))
    return job_id


def _items(campaign_id: str) -> dict[str, dict]:
    return {i["adapter_key"]: i for i in campaign_repo.list_plan_items(campaign_id)}


@register_adapter
class _Board(SourceAdapter):
    """An ordinary board whose endpoint can answer 403, 200 or anything else."""

    key = "decline.board"
    display_name = "Decline Board"
    source_type = SourceType.JOB_BOARD
    coverage_countries: list[str] = ["BE"]
    capabilities = AdapterCapabilities(pagination=False)
    URL = "https://decline.test/jobs"
    calls: list[dict] = []

    def plan(self, directives, composite_profile, caps):
        return [PlanItem(adapter_key=self.key, native_query={"queries": ["data"]})]

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        type(self).calls.append(dict(item.native_query))
        result = await self.egress.fetch(self.URL)
        if not result.ok:
            raise SourceUnavailable(f"[{self.key}] refused with HTTP {result.status_code}")
        return [RawRecord(
            url=self.URL,
            content="{}",
            meta={"title": "Data Engineer", "company_name_raw": "Decline NV", "country": "BE"},
        )]

    def parse(self, raw: RawRecord) -> list[dict]:
        return [dict(raw.meta)]

    def normalise(self, parsed: dict, raw: RawRecord) -> NormalisedRecord | None:
        return NormalisedRecord(entity_type="vacancy", data=dict(parsed))


@register_adapter
class _Healthy(SourceAdapter):
    key = "decline.healthy"
    display_name = "Decline Healthy"
    source_type = SourceType.JOB_BOARD
    coverage_countries: list[str] = ["BE"]
    capabilities = AdapterCapabilities(pagination=False)

    def plan(self, directives, composite_profile, caps):
        return [PlanItem(adapter_key=self.key, native_query={"queries": ["data"]})]

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        result = await self.egress.fetch("https://healthy.test/jobs")
        if not result.ok:
            raise SourceUnavailable(f"refused with HTTP {result.status_code}")
        return [RawRecord(
            url="https://healthy.test/jobs",
            content="{}",
            meta={"title": "Platform Engineer", "company_name_raw": "Healthy NV"},
        )]

    def parse(self, raw: RawRecord) -> list[dict]:
        return [dict(raw.meta)]

    def normalise(self, parsed: dict, raw: RawRecord) -> NormalisedRecord | None:
        return NormalisedRecord(entity_type="vacancy", data=dict(parsed))


@register_adapter
class _Wide(SourceAdapter):
    """A source that plans more pages than the campaign's cap allows."""

    key = "decline.wide"
    display_name = "Decline Wide"
    source_type = SourceType.JOB_BOARD
    coverage_countries: list[str] = ["BE"]
    capabilities = AdapterCapabilities()

    def plan(self, directives, composite_profile, caps):
        return [
            PlanItem(adapter_key=self.key, native_query={"queries": [f"role{index}"]},
                     estimated_pages=2)
            for index in range(4)
        ]

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        return []

    def parse(self, raw: RawRecord) -> list[dict]:
        return []

    def normalise(self, parsed: dict, raw: RawRecord) -> NormalisedRecord | None:
        return None


def _observation(**overrides) -> SimpleNamespace:
    """A settled outcome, as the decline policy reads one."""
    base = {
        "robots_blocked": 0,
        "blocked_status": None,
        "unauthorized": 0,
        "failed_requests": 0,
        "blocked": 0,
        "written": 0,
        "requests": 0,
        "block_reason": None,
        "last_error": None,
        "evidence_url": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# The policy, without a database
# ---------------------------------------------------------------------------


def test_adapter_declines_after_three_refusals_in_one_run():
    reading = declines.observation_of(
        _observation(blocked=3, requests=3, blocked_status=403,
                     evidence_url="https://decline.test/jobs"),
        adapter_key="decline.board",
    )
    assert reading is not None and reading.reason == declines.REASON_HTTP_403
    assert reading.refusals == 3
    declined, runs = declines.should_decline(reading, None, campaign_id="c1")
    assert declined is True and runs == 1


def test_adapter_one_refusal_is_evidence_and_two_runs_decline():
    """A first refusal is recorded; the second campaign confirms it."""
    reading = declines.observation_of(
        _observation(blocked=1, requests=1, blocked_status=403),
        adapter_key="decline.board",
    )
    declined, _ = declines.should_decline(reading, None, campaign_id="c1")
    assert declined is False, "one refused page is not a systematic refusal"

    again, runs = declines.should_decline(
        reading,
        {"run_count": 1, "last_campaign_id": "c1", "refused_count": 1},
        campaign_id="c2",
    )
    assert again is True and runs == 2, "the same fact in two campaigns is confirmed"


def test_adapter_that_collected_anything_is_never_declined():
    assert declines.observation_of(
        _observation(blocked=5, written=2, requests=6, blocked_status=403),
        adapter_key="decline.board",
    ) is None


def test_adapter_real_failure_vetoes_a_decline():
    """A 5xx alongside a 403 is an outage, and hiding it is the old bug."""
    assert declines.observation_of(
        _observation(blocked=5, failed_requests=1, requests=6, blocked_status=403),
        adapter_key="decline.board",
    ) is None


def test_adapter_that_wrote_records_this_run_is_not_declined(db):
    """A per-tenant vendor with dead boards is still serving live tenants."""
    outcome = _observation(blocked=5, requests=5, blocked_status=403)
    assert declines.observe(_Board.key, outcome, "c1", had_success=True) is None
    assert decline_repo.get_decline(_Board.key) is None

    # Without a single record anywhere in the run, the same evidence declines.
    declines.observe(_Board.key, outcome, "c1", had_success=False)
    assert decline_repo.get_active_decline(_Board.key) is not None


def test_adapter_401_everywhere_is_read_as_refused():
    reading = declines.observation_of(
        _observation(unauthorized=3, failed_requests=3, requests=3),
        adapter_key="decline.keyless",
    )
    assert reading is not None and reading.reason == declines.REASON_HTTP_401
    declined, _ = declines.should_decline(reading, None, campaign_id="c1")
    assert declined is True


def test_adapter_terms_declined_immediately_and_robots_is_a_reason():
    terms = declines.Observation(
        adapter_key="board.indeed", reason=declines.REASON_TERMS, detail="tos",
        evidence_url=None, requests=0, refusals=0,
    )
    declined, _ = declines.should_decline(terms, None, campaign_id=None)
    assert declined is True, "IR-101 needs no request threshold"

    robots = declines.observation_of(
        _observation(robots_blocked=1, blocked=1, requests=1),
        adapter_key="board.x",
    )
    assert robots is not None and robots.reason == declines.REASON_ROBOTS


# ---------------------------------------------------------------------------
# Across a run: the adapter is declined, and skipped next time
# ---------------------------------------------------------------------------


def test_collection_persistent_403_declines_the_adapter(db, answers):
    answers(403)
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue(_Board.key)
    for index in range(3):
        _plan_item(campaign_id, _Board.key, {"queries": [f"role{index}"]})

    _run(campaign_id, seeker)

    row = decline_repo.get_decline(_Board.key)
    assert row is not None, "the pattern has to outlive the campaign that saw it"
    assert row["reason"] == declines.REASON_HTTP_403
    assert row["declined_at"] and not row["acknowledged_at"]
    assert row["refused_count"] == 3 and row["run_count"] == 1
    assert "HTTP 403" in (row["detail"] or "")
    assert row["evidence_url"]
    assert query_all("SELECT id FROM audit_event WHERE action = 'source.declined'")

    for item in _items(campaign_id).values():
        assert item["status"] == "blocked", "the page verdict is unchanged"
        assert item["error_count"] == 0, "a refusal is still not an error"
    job = query_one("SELECT error_count FROM job_run WHERE kind = 'collection'")
    assert job["error_count"] == 0


def test_collection_single_403_does_not_decline_the_adapter(db, answers):
    """Item 5: one refused page keeps its per-page behaviour and nothing more."""
    answers(403)
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue(_Board.key)
    _plan_item(campaign_id, _Board.key, {"queries": ["data"]})

    _run(campaign_id, seeker)

    item = _items(campaign_id)[_Board.key]
    assert item["status"] == "blocked" and item["error_count"] == 0
    row = decline_repo.get_decline(_Board.key)
    assert row is not None, "the refusal is evidence for the next campaign"
    assert row["declined_at"] is None, "one refusal is not a decline"
    assert decline_repo.get_active_decline(_Board.key) is None


def test_collection_skips_declined_without_charging_pages_or_errors(db, monkeypatch):
    decline_repo.record_observation(
        _Board.key,
        reason=declines.REASON_HTTP_403,
        detail="https://decline.test/jobs: HTTP 403",
        evidence_url="https://decline.test/jobs",
        requests=10,
        refusals=10,
        campaign_id="an-earlier-campaign",
        declined=True,
    )
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue(_Board.key)
    _catalogue(_Healthy.key)
    _plan_item(campaign_id, _Board.key, {"queries": ["data"]})
    _plan_item(campaign_id, _Healthy.key, {"queries": ["data"]})

    async def _fetch(self, url, **kwargs):
        return _answer(url, 403 if "decline.test" in url else 200)

    monkeypatch.setattr(EgressClient, "fetch", _fetch)
    _Board.calls.clear()

    job_id = _run(campaign_id, seeker)

    declined_item = _items(campaign_id)[_Board.key]
    assert declined_item["status"] == "planned", "re-enabling must make it runnable again"
    assert declined_item["outcome_state"] == "skipped"
    assert "declined" in (declined_item["outcome_reason"] or "")
    assert declined_item["error_count"] == 0
    assert declined_item["records_collected"] == 0
    assert _Board.calls == [], "no request is issued for a declined source"
    # Nothing was charged, so the run's own counters stay clean.
    job = query_one("SELECT error_count, progress_done FROM job_run WHERE id = ?", (job_id,))
    assert job["error_count"] == 0

    healthy = _items(campaign_id)[_Healthy.key]
    assert healthy["status"] == "done" and healthy["records_collected"] == 1


def test_planning_next_plan_excludes_declined_source(db):
    decline_repo.record_observation(
        _Board.key,
        reason=declines.REASON_HTTP_403,
        detail="https://decline.test/jobs: HTTP 403",
        evidence_url="https://decline.test/jobs",
        requests=5,
        refusals=5,
        campaign_id="an-earlier-campaign",
        declined=True,
    )
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue(_Board.key)
    _catalogue(_Healthy.key)

    summary = planning.generate_plan(
        campaign_id, seeker, use_llm=False, assess_knowledge_base=False
    )

    keys = {i["adapter_key"] for i in summary["items"]}
    assert _Healthy.key in keys
    assert _Board.key not in keys, "a declined source is not planned again"
    rejected = {r["adapter_key"]: r for r in summary["rejected_sources"]}
    assert "declined" in rejected[_Board.key]["reason"]
    assert rejected[_Board.key]["declined"]["reason"] == declines.REASON_HTTP_403
    assert summary["declined_sources"] == [rejected[_Board.key]["declined"]]


# ---------------------------------------------------------------------------
# IR-101: terms stay declined until an administrator says otherwise
# ---------------------------------------------------------------------------


def test_planning_terms_decline_stays_until_acknowledged(db):
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue(_Board.key, tos_status="prohibited", requires_ack=1)

    with pytest.raises(planning.NoUsableSources) as first:
        planning.generate_plan(campaign_id, seeker, use_llm=False, assess_knowledge_base=False)
    assert _Board.key in str(first.value)
    row = decline_repo.get_decline(_Board.key)
    assert row["reason"] == declines.REASON_TERMS
    assert row["declined_at"], "terms are declined before a request is ever issued"

    with pytest.raises(planning.NoUsableSources):
        planning.generate_plan(campaign_id, seeker, use_llm=False, assess_knowledge_base=False)
    assert decline_repo.get_active_decline(_Board.key), "it stays declined until acknowledged"

    # Clearing the decline is not the same as acknowledging the terms: the
    # catalogue row still prohibits access, so the next plan declines it again.
    declines.clear(_Board.key, actor="admin@example.com", note="trying again")
    assert decline_repo.get_active_decline(_Board.key) is None
    with pytest.raises(planning.NoUsableSources):
        planning.generate_plan(campaign_id, seeker, use_llm=False, assess_knowledge_base=False)
    assert decline_repo.get_active_decline(_Board.key), "IR-101 needs the acknowledgement"

    # The administrator acknowledges the terms and re-enables the source.
    admin_repo.update_source(_Board.key, {"acknowledged_at": utcnow(), "enabled": 1})
    declines.clear(_Board.key, actor="admin@example.com", note="licence on file")
    summary = planning.generate_plan(
        campaign_id, seeker, use_llm=False, assess_knowledge_base=False
    )
    assert _Board.key in {i["adapter_key"] for i in summary["items"]}
    cleared = decline_repo.get_decline(_Board.key)
    assert cleared["acknowledged_by"] == "admin@example.com"
    assert cleared["refused_count"] == 0, "the second chance starts from no evidence"
    assert query_all(
        "SELECT id FROM audit_event WHERE action = 'admin.source_decline_cleared'"
    )


# ---------------------------------------------------------------------------
# Zero usable sources: a blocker, not an empty campaign
# ---------------------------------------------------------------------------


def test_planning_all_sources_declined_fails_with_reasons(db):
    decline_repo.record_observation(
        _Board.key,
        reason=declines.REASON_HTTP_403,
        detail="https://decline.test/jobs: HTTP 403",
        evidence_url="https://decline.test/jobs",
        requests=5,
        refusals=5,
        campaign_id="an-earlier-campaign",
        declined=True,
    )
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue(_Board.key)

    with pytest.raises(planning.NoUsableSources) as caught:
        planning.generate_plan(campaign_id, seeker, use_llm=False, assess_knowledge_base=False)

    message = str(caught.value)
    assert "No usable source" in message and _Board.key in message
    assert caught.value.blockers[0]["adapter_key"] == _Board.key
    assert caught.value.blockers[0]["reason"] == declines.REASON_HTTP_403
    blocked = query_one("SELECT detail FROM audit_event WHERE action = 'campaign.plan_blocked'")
    assert blocked is not None and _Board.key in blocked["detail"]


def test_collection_launch_refuses_all_declined_sources(db):
    decline_repo.record_observation(
        _Board.key,
        reason=declines.REASON_HTTP_403,
        detail="https://decline.test/jobs: HTTP 403",
        evidence_url="https://decline.test/jobs",
        requests=5,
        refusals=5,
        campaign_id="an-earlier-campaign",
        declined=True,
    )
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue(_Board.key)
    _plan_item(campaign_id, _Board.key, {"queries": ["data"]})

    with pytest.raises(collection.NoUsableSources) as caught:
        asyncio.run(collection.launch(campaign_id, seeker))

    assert _Board.key in str(caught.value)
    assert not query_all("SELECT id FROM job_run WHERE campaign_id = ?", (campaign_id,))


# ---------------------------------------------------------------------------
# The cap: one source of truth, configurable, and surfaced (FR-186)
# ---------------------------------------------------------------------------


def test_planning_autopilot_cap_is_one_decision():
    assert autopilot.DEFAULT_CAPS_OVERRIDE is planning.DEFAULT_AUTOPILOT_CAPS
    assert planning.DEFAULT_AUTOPILOT_CAPS["max_pages"] == 400
    # The plan defaults stay a supervised sweep's budget; autopilot tightens.
    assert planning.DEFAULT_CAPS["max_pages"] == 10_000


def test_planning_cap_is_honoured_and_scale_down_surfaced(db):
    seeker = _seeker()
    campaign_id = _campaign(
        seeker, caps={"max_pages": 3, "max_pages_per_source": 2}
    )
    _catalogue(_Wide.key)

    summary = planning.generate_plan(
        campaign_id, seeker, use_llm=False, assess_knowledge_base=False
    )

    budget = summary["page_budget"]
    assert budget == {"requested": 8, "granted": 3, "max_pages": 3}, budget
    assert summary["plan_notice"]["page_budget"]["scaled_down"] is True
    assert "8 pages under a 3-page cap" in summary["plan_notice"]["message"]
    assert sum(i["estimated_pages"] for i in summary["items"]) == 3

    # Both screens the user reads carry it: the plan review and the run report.
    plan = planning.plan_summary(campaign_id, seeker)
    assert plan["page_budget"]["granted"] == 3
    assert "scaled down" in (plan["plan_notice"]["message"] or "").lower() or (
        "3-page cap" in (plan["plan_notice"]["message"] or "")
    )
    status = collection.status(campaign_id, seeker)
    assert status["plan_notice"]["page_budget"]["max_pages"] == 3

    # The cap is the campaign's, not a constant: a wider campaign is not scaled.
    other = _campaign(
        seeker, profile_version=2, caps={"max_pages": 10, "max_pages_per_source": 2}
    )
    wider = planning.generate_plan(other, seeker, use_llm=False, assess_knowledge_base=False)
    assert wider["page_budget"] == {"requested": 8, "granted": 8, "max_pages": 10}
    assert wider["plan_notice"]["page_budget"]["scaled_down"] is False
    assert wider["plan_notice"]["message"] is None
