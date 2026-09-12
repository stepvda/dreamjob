"""Progress counts planned pages, so the bar can never pass 100% (FR-185).

A real collection job reported ``progress_done=244 / progress_total=85`` and
climbing.  The denominator counted the plan's *pages* (``stats.pages_done``
now, ``stats.pages + sum(remaining)`` then) while the numerator counted
*charged requests* (FR-186's budget), and one planned page may issue several
requests - a website crawl up to thirty, a partitioned search a handful.  An
adapter that issued three requests per page therefore read as 300% of a
two-page plan.

The test installs exactly that adapter: one planned page, three egress
requests.  It pins three things: the charged budget really was 6 (the old
numerator), the bar's numerator stayed on planned pages, and every value the
worker published was monotonic and never above its denominator.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

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
from dreamjob.db.connection import insert_row, query_one, upsert_row, utcnow
from dreamjob.db.migrator import migrate
from dreamjob.db.repositories import campaigns as campaign_repo
from dreamjob.egress.client import EgressClient, FetchResult
from dreamjob.jobs.runner import JobContext, runner
from dreamjob.pipeline import collection


@pytest.fixture()
def db(tmp_path, monkeypatch) -> Iterator[None]:
    monkeypatch.setenv("DREAMJOB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DREAMJOB_DB_PATH", str(tmp_path / "data" / "test.db"))
    get_settings.cache_clear()
    get_settings()
    migrate()
    yield
    get_settings.cache_clear()


@register_adapter
class _ChattyBoard(SourceAdapter):
    """One planned page, three egress requests (FR-186 charges all three)."""

    key = "progress.chatty"
    display_name = "Chatty Board"
    source_type = SourceType.JOB_BOARD
    capabilities = AdapterCapabilities(pagination=True)
    URL = "https://progress.test/jobs"

    def plan(self, directives, composite_profile, caps):
        return []

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        for slice_no in range(3):
            await self.egress.fetch(f"{self.URL}?slice={slice_no}")
        return [
            RawRecord(
                url=self.URL,
                content="{}",
                meta={"title": "Data Engineer", "company_name_raw": "Progress NV",
                      "country": "BE"},
            )
        ]

    def parse(self, raw: RawRecord) -> list[dict]:
        return [dict(raw.meta)]

    def normalise(self, parsed: dict, raw: RawRecord) -> NormalisedRecord | None:
        return NormalisedRecord(entity_type="vacancy", data=dict(parsed))


def _seeker() -> str:
    return insert_row(
        "job_seeker",
        {"email": "progress@example.com", "display_name": "Progress",
         "created_at": utcnow(), "updated_at": utcnow()},
    )


def _campaign(seeker_id: str) -> str:
    directive_id = insert_row(
        "directive_set",
        {"job_seeker_id": seeker_id, "name": "Progress", "created_at": utcnow(),
         "job_content": {"target_titles": ["Data Engineer"]},
         "location": {"countries": ["BE"]}},
    )
    profile_id = insert_row(
        "profile_version",
        {"job_seeker_id": seeker_id, "version": 1, "sections": {}, "created_at": utcnow()},
    )
    return campaign_repo.create_campaign(
        seeker_id,
        {"name": "Progress", "directive_set_id": directive_id,
         "profile_version_id": profile_id,
         "caps": {"max_pages": 20, "max_pages_per_source": 20}},
    )


def _catalogue() -> None:
    upsert_row(
        "source_catalogue",
        {
            "adapter_key": _ChattyBoard.key,
            "display_name": _ChattyBoard.display_name,
            "source_type": "job_board",
            "coverage_countries": ["BE"],
            "coverage_industries": [],
            "query_capabilities": AdapterCapabilities(pagination=True).__dict__,
            "access_method": "api",
            "rate_limit_rps": 1.0,
            "cost_per_call_eur": 0.0,
            "tos_status": "permitted",
            "enabled": 1,
            "requires_ack": 0,
            "updated_at": utcnow(),
        },
        ["adapter_key"],
    )


@pytest.fixture()
def answers(monkeypatch) -> None:
    """Every egress request answers 200 without touching the network."""

    async def _fetch(self, url: str, **kwargs: Any) -> FetchResult:
        return FetchResult(
            url=url, status_code=200, text="{}", content=b"{}", headers={},
            from_cache=False, raw_document_id=None, content_hash="",
        )

    monkeypatch.setattr(EgressClient, "fetch", _fetch)


def test_progress_counts_planned_pages_not_charged_requests(db, answers) -> None:
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue()
    item_id = campaign_repo.insert_plan_item(
        campaign_id,
        {"adapter_key": _ChattyBoard.key, "native_query": {"keywords": ["data"]},
         "estimated_pages": 2, "created_at": utcnow()},
    )

    published: list[tuple[int, int]] = []
    job_id = runner.create(collection.JOB_KIND, campaign_id=campaign_id, job_seeker_id=seeker)
    ctx = JobContext(job_id=job_id, campaign_id=campaign_id, job_seeker_id=seeker)
    original = ctx.progress

    def spy(done: int, total: int | None = None) -> None:
        if total is not None:
            published.append((int(done), int(total)))
        original(done, total)

    ctx.progress = spy  # type: ignore[method-assign]
    asyncio.run(collection.collection_worker(ctx))

    # The scenario is real: two planned pages cost six requests, so the old
    # numerator (the charged budget) would have published 6 of 2.
    done = campaign_repo.get_plan_item(item_id)
    assert done["status"] == "done"
    assert (done["caps"] or {})["outcome"]["charged_pages"] == 6, (
        "the adapter must have been charged three requests on each of two pages"
    )

    assert published, "the worker must publish progress at least once"
    assert published[-1] == (2, 2), f"planned pages finish at 2 of 2: {published}"
    assert all(seen <= total for seen, total in published), (
        f"progress_done exceeded progress_total: {published}"
    )
    values = [seen for seen, _ in published]
    assert values == sorted(values), f"progress went backwards: {published}"

    job = query_one("SELECT progress_done, progress_total FROM job_run WHERE id = ?", (job_id,))
    assert (job["progress_done"], job["progress_total"]) == (2, 2)
