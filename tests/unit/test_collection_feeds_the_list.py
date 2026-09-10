"""Collection hands its vacancies to the ranked list, in full (FR-261, FR-166).

Two defects are pinned here, and they compounded into one symptom: an account
with 42,883 collected vacancies and an Opportunities screen reading "0".

* collection ended at the knowledge base.  Synthesis - the pass that turns a
  vacancy row into an opportunity - waited for a button, and the Opportunities
  screen only offers that button once a single campaign is selected, which is
  not the default.  So the ranked list stayed empty and nothing said why;
* ``campaign_vacancies`` capped its pool at 5,000 rows, so even once synthesis
  did run, a long collection was silently truncated to the newest eighth of
  itself.
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
from dreamjob.config import get_settings
from dreamjob.db.connection import insert_row, query_all, upsert_row, utcnow, write_tx
from dreamjob.db.migrator import migrate
from dreamjob.db.repositories import campaigns as campaign_repo
from dreamjob.db.repositories import opportunities as opp_repo
from dreamjob.jobs.runner import JobContext, runner
from dreamjob.pipeline import collection, planning


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


def _seeker() -> str:
    return insert_row(
        "job_seeker",
        {
            "email": "feeds@example.com",
            "display_name": "Feeds",
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )


def _campaign(seeker_id: str) -> str:
    directive_id = insert_row(
        "directive_set",
        {
            "job_seeker_id": seeker_id,
            "name": "Feeds",
            "created_at": utcnow(),
            "job_content": {"target_titles": ["Data Engineer"], "titles": ["Data Engineer"]},
            # No countries and no geocoded areas: nothing here is a hard
            # exclusion, so what the run collects is what synthesis should see.
            "location": {"areas": [], "countries": [], "commute_mode": "car"},
        },
    )
    profile_id = insert_row(
        "profile_version",
        {"job_seeker_id": seeker_id, "version": 1, "sections": {}, "created_at": utcnow()},
    )
    return campaign_repo.create_campaign(
        seeker_id,
        {
            "name": "Feeds",
            "directive_set_id": directive_id,
            "profile_version_id": profile_id,
            "caps": {"max_pages": 20, "max_pages_per_source": 3},
        },
    )


def _catalogue(adapter_key: str) -> None:
    upsert_row(
        "source_catalogue",
        {
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
        },
        ["adapter_key"],
    )


def _run(campaign_id: str, seeker: str) -> JobContext:
    job_id = runner.create(collection.JOB_KIND, campaign_id=campaign_id, job_seeker_id=seeker)
    ctx = JobContext(
        job_id=job_id, campaign_id=campaign_id, job_seeker_id=seeker, checkpoint={}
    )
    asyncio.run(collection.collection_worker(ctx))
    return ctx


class _Base(SourceAdapter):
    def plan(self, directives: dict, composite_profile: dict, caps: dict) -> list[PlanItem]:
        return []

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        return []

    def parse(self, raw: RawRecord) -> list[dict]:
        return []

    def normalise(self, parsed: dict, raw: RawRecord) -> NormalisedRecord | None:
        return NormalisedRecord(entity_type="vacancy", data=dict(parsed))


@register_adapter
class _FeedsBoard(_Base):
    """A board that returns three usable vacancies on one page."""

    key = "feeds.board"
    display_name = "Feeds Board"
    source_type = SourceType.JOB_BOARD

    def plan(self, directives: dict, composite_profile: dict, caps: dict) -> list[PlanItem]:
        return [
            PlanItem(
                adapter_key=self.key,
                native_query={"queries": ["Data Engineer"], "pages": 1},
                estimated_pages=1,
                rationale="keyword search",
            )
        ]

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        return [RawRecord(url="https://feeds.test/jobs", content="<html/>", meta={"page": 1})]

    def parse(self, raw: RawRecord) -> list[dict]:
        return [
            {
                "title": f"Data Engineer {n}",
                "company_name_raw": "Feeds NV",
                "location": "Brussels",
                "country": "BE",
                "source_url": f"https://feeds.test/jobs/{n}",
                "posted_at": "2026-09-01",
            }
            for n in range(3)
        ]


@register_adapter
class _SilentFeedsBoard(_Base):
    """A board that fetches nothing at all: the run collects no records."""

    key = "feeds.silent"
    display_name = "Silent Feeds Board"
    source_type = SourceType.JOB_BOARD

    def plan(self, directives: dict, composite_profile: dict, caps: dict) -> list[PlanItem]:
        return [
            PlanItem(
                adapter_key=self.key,
                native_query={"queries": ["Data Engineer"], "pages": 1},
                estimated_pages=1,
                rationale="keyword search",
            )
        ]


# ---------------------------------------------------------------------------
# Collection now finishes the job it started
# ---------------------------------------------------------------------------


def test_a_finished_collection_leaves_opportunities_on_the_ranked_list(db):
    """The defect: 42,883 vacancies collected, and the list still read zero."""
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("feeds.board")
    planning.generate_plan(campaign_id, seeker, use_llm=False)

    ctx = _run(campaign_id, seeker)

    vacancies = query_all("SELECT id FROM vacancy")
    assert len(vacancies) == 3, "the run collected what the board offered"

    opportunities = query_all(
        "SELECT title FROM opportunity WHERE campaign_id = ?", (campaign_id,)
    )
    assert len(opportunities) == 3, "and every one of them reached the ranked list"
    assert ctx.checkpoint["synthesis"]["created"] == 3
    assert ctx.checkpoint["synthesis"]["rejected"] == 0


def test_the_synthesis_that_ran_is_on_the_audit_trail(db):
    """A pass that runs without being asked has to say that it ran (FR-185)."""
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("feeds.board")
    planning.generate_plan(campaign_id, seeker, use_llm=False)

    _run(campaign_id, seeker)

    actions = [
        r["action"]
        for r in query_all(
            "SELECT action FROM audit_event WHERE entity_id = ? ORDER BY created_at", (campaign_id,)
        )
    ]
    assert "campaign.synthesis_finished" in actions
    assert actions.index("campaign.collection_finished") < actions.index(
        "campaign.synthesis_finished"
    ), "synthesis follows the collection whose records it normalised"


def test_the_opportunity_ids_are_kept_out_of_the_job_row(db):
    """A 40k-vacancy campaign must not write 40k ids into its checkpoint."""
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("feeds.board")
    planning.generate_plan(campaign_id, seeker, use_llm=False)

    ctx = _run(campaign_id, seeker)

    assert "opportunity_ids" not in ctx.checkpoint["synthesis"]


def test_a_run_that_collected_nothing_does_not_synthesise(db):
    """Nothing to normalise, so nothing is claimed to have been normalised."""
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("feeds.silent")
    planning.generate_plan(campaign_id, seeker, use_llm=False)

    ctx = _run(campaign_id, seeker)

    assert query_all("SELECT id FROM vacancy") == []
    assert ctx.checkpoint["synthesis"] is None
    actions = [
        r["action"] for r in query_all("SELECT action FROM audit_event WHERE entity_id = ?", (campaign_id,))
    ]
    assert "campaign.synthesis_finished" not in actions


def test_a_synthesis_that_falls_over_does_not_fail_the_collection(db, monkeypatch):
    """The records were written and the run was good: that stands on its own."""
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("feeds.board")
    planning.generate_plan(campaign_id, seeker, use_llm=False)

    from dreamjob.pipeline import opportunities as opp_mod

    def explode(campaign, **kwargs):
        raise RuntimeError("synthesis is broken")

    monkeypatch.setattr(opp_mod, "synthesise_campaign", explode)

    ctx = _run(campaign_id, seeker)

    assert len(query_all("SELECT id FROM vacancy")) == 3, "the collection still stands"
    assert campaign_repo.get_campaign(campaign_id, seeker)["status"] == "completed"
    assert "synthesis is broken" in ctx.checkpoint["synthesis"]["error"]
    actions = [
        r["action"] for r in query_all("SELECT action FROM audit_event WHERE entity_id = ?", (campaign_id,))
    ]
    assert "campaign.synthesis_failed" in actions, "the failure is visible, not swallowed"


# ---------------------------------------------------------------------------
# The pool is the whole collection, not the newest 5,000 of it
# ---------------------------------------------------------------------------


def _bulk_vacancies(campaign_id: str, count: int) -> None:
    """Write ``count`` vacancies linked to one plan item of this campaign."""
    plan_item_id = campaign_repo.insert_plan_item(
        campaign_id,
        {"adapter_key": "feeds.board", "native_query": {}, "estimated_pages": 1},
    )
    now = utcnow()
    with write_tx() as conn:
        for n in range(count):
            vacancy_id = f"v{n:07d}"
            conn.execute(
                "INSERT INTO vacancy (id, title, company_name_raw, location, country, "
                "dedup_key, collected_at, posted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (vacancy_id, f"Data Engineer {n}", "Bulk NV", "Brussels", "BE",
                 f"dedup-{n}", now, f"2026-01-{(n % 28) + 1:02d}"),
            )
            conn.execute(
                "INSERT INTO provenance (id, entity_type, entity_id, source_plan_item_id, "
                "created_at) VALUES (?, 'vacancy', ?, ?, ?)",
                (f"p{n:07d}", vacancy_id, plan_item_id, now),
            )


def test_the_pool_is_not_capped_at_five_thousand(db):
    """A campaign that collected more than 5,000 vacancies means to rank them."""
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _bulk_vacancies(campaign_id, 5_200)

    pool = opp_repo.campaign_vacancies(campaign_id)

    assert len(pool) == 5_200, "the newest 5,000 is not the whole collection"
