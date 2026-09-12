"""An unavailable source is skipped once, not planned, and never an error.

``directory.opencorporates`` needs an API token this deployment does not hold.
The registry adapter's own ``plan()`` answers ``[]`` for that, but the planner
used the adapter's *keyword-searchable* catalogue shape to keep it and wrote a
fall-back query anyway - so every run planned one unusable item, collection
raised ``UnusableQuery`` once per page, and the generic page handler logged an
ERROR with a traceback and counted the missing key as a collection fault.

Two fixes are pinned here:

* planning rejects an adapter whose ``available()`` is false, before it can be
  charged anything, with the reason on the review screen's rejected list and a
  single INFO line;
* collection, meeting an item from an older plan, settles it ``skipped`` with
  the configuration an administrator can act on, leaves it runnable for the
  run after the key is configured, logs once per source per plan, and
  increments no error counter.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator

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
from dreamjob.adapters.query_errors import UnusableQuery
from dreamjob.config import get_settings
from dreamjob.db.connection import insert_row, query_one, upsert_row, utcnow
from dreamjob.db.migrator import migrate
from dreamjob.db.repositories import campaigns as campaign_repo
from dreamjob.jobs.runner import JobContext, runner
from dreamjob.pipeline import collection, planning


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
class _KeylessDirectory(SourceAdapter):
    """A keyword-searchable directory whose required key is absent (FR-245)."""

    key = "planning.keyless"
    display_name = "Keyless Directory"
    source_type = SourceType.DIRECTORY
    coverage_countries: list[str] = ["BE"]
    capabilities = AdapterCapabilities(keyword_search=True)

    def available(self) -> bool:
        return False

    def unavailable_reason(self) -> str:
        return (
            "Keyless Directory needs an API key; set PLANNING_TEST_KEY in the environment "
            "or store it as the app setting registry.secret.planning_test_key"
        )

    def plan(self, directives, composite_profile, caps):
        # The registry base answers [] when it is not available; planning must
        # not turn that into a fall-back query (the bug this file pins).
        return []

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        raise UnusableQuery(self.key, self.unavailable_reason())

    def parse(self, raw: RawRecord) -> list[dict]:
        return []

    def normalise(self, parsed: dict, raw: RawRecord) -> NormalisedRecord | None:
        return None


def _seeker() -> str:
    return insert_row(
        "job_seeker",
        {"email": "keyless@example.com", "display_name": "Keyless",
         "created_at": utcnow(), "updated_at": utcnow()},
    )


def _campaign(seeker_id: str) -> str:
    directive_id = insert_row(
        "directive_set",
        {"job_seeker_id": seeker_id, "name": "Keyless", "created_at": utcnow(),
         "job_content": {"target_titles": ["Data Engineer"]},
         "location": {"countries": ["Belgium"]}},
    )
    profile_id = insert_row(
        "profile_version",
        {"job_seeker_id": seeker_id, "version": 1, "sections": {}, "created_at": utcnow()},
    )
    return campaign_repo.create_campaign(
        seeker_id,
        {"name": "Keyless", "directive_set_id": directive_id,
         "profile_version_id": profile_id,
         "caps": {"max_pages": 20, "max_pages_per_source": 5}},
    )


def _catalogue() -> None:
    upsert_row(
        "source_catalogue",
        {
            "adapter_key": _KeylessDirectory.key,
            "display_name": _KeylessDirectory.display_name,
            "source_type": "directory",
            "coverage_countries": ["BE"],
            "coverage_industries": [],
            "query_capabilities": AdapterCapabilities(keyword_search=True).__dict__,
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


def test_an_unavailable_adapter_is_not_planned(db, caplog) -> None:
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue()

    with caplog.at_level(logging.INFO, logger="dreamjob.pipeline.planning"):
        summary = planning.generate_plan(
            campaign_id, seeker, use_llm=False, assess_knowledge_base=False
        )

    assert _KeylessDirectory.key not in [
        item["adapter_key"] for item in campaign_repo.list_plan_items(campaign_id)
    ], "an adapter with no key must not be charged a plan item"
    rejected = {r["adapter_key"]: r["reason"] for r in summary["rejected_sources"]}
    assert _KeylessDirectory.key in rejected
    assert "needs an API key" in rejected[_KeylessDirectory.key]
    lines = [
        record.getMessage()
        for record in caplog.records
        if _KeylessDirectory.key in record.getMessage() and "not planned" in record.getMessage()
    ]
    assert len(lines) == 1, f"the skip is narrated once, not per item: {lines}"


def test_collection_skips_an_unavailable_item_once_without_errors(db, caplog) -> None:
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue()
    for index in range(2):
        campaign_repo.insert_plan_item(
            campaign_id,
            {"adapter_key": _KeylessDirectory.key,
             "native_query": {"query": f"part{index}"},
             "estimated_pages": 1, "created_at": utcnow()},
        )

    job_id = runner.create(collection.JOB_KIND, campaign_id=campaign_id, job_seeker_id=seeker)
    ctx = JobContext(job_id=job_id, campaign_id=campaign_id, job_seeker_id=seeker)
    with caplog.at_level(logging.INFO, logger="dreamjob.pipeline.collection"):
        asyncio.run(collection.collection_worker(ctx))

    items = campaign_repo.list_plan_items(campaign_id)
    assert len(items) == 2
    for item in items:
        assert item["status"] == "planned", "left runnable for the run after the key is configured"
        assert item["outcome_state"] == "skipped"
        assert "needs an API key" in (item["outcome_reason"] or item["last_error"] or "")
        assert item["error_count"] == 0

    job = query_one("SELECT error_count FROM job_run WHERE id = ?", (job_id,))
    assert job["error_count"] == 0, "a disabled adapter is not a collection error"

    lines = [
        record.getMessage()
        for record in caplog.records
        if "skipped in collection" in record.getMessage()
    ]
    assert len(lines) == 1, f"one line per source per plan, however many items: {lines}"
