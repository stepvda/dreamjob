"""The plan and vacancy-aggregate indexes added by migration 152 (NFR-101, NFR-102).

Two list paths paid for an index they did not have:

* ``list_plan_items`` reads every ``source_plan_item`` of one campaign in
  ``created_at, adapter_key`` order.  The only campaign index was
  ``(campaign_id, status)``, so the planner fetched the campaign's rows and
  sorted them in a temporary B-tree on every read.
* the all-companies sweep counted and dated each company's vacancies with two
  correlated subqueries.  Folding them into one grouped read evaluates
  ``MAX(COALESCE(posted_at, collected_at))``, which reads either column and no
  existing ``vacancy`` index covered, so the grouped read degenerated into a
  full scan of the wide ``vacancy`` table; 152 adds a covering index for it.

The second half of the file reads ``EXPLAIN QUERY PLAN``.  That is the only
honest way to tell the index is *used*: a query that is correct, and fast on a
three-row fixture, says nothing about the plan the live fifty-thousand-row
corpus gets.  The tests assert the plan SQLite reports, not a stopwatch.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from dreamjob.config import get_settings
from dreamjob.db.connection import insert_row, query_all, query_one, utcnow
from dreamjob.db.migrator import migrate

PLAN_QUERY = (
    "SELECT * FROM source_plan_item WHERE campaign_id = ? "
    "ORDER BY created_at, adapter_key"
)


@pytest.fixture()
def db(tmp_path, monkeypatch) -> Iterator[None]:
    monkeypatch.setenv("DREAMJOB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DREAMJOB_DB_PATH", str(tmp_path / "data" / "test.db"))
    get_settings.cache_clear()
    get_settings().ensure_dirs()
    migrate()
    yield
    get_settings.cache_clear()


def _campaign() -> str:
    now = utcnow()
    seeker_id = insert_row(
        "job_seeker",
        {"email": "indexes@example.org", "display_name": "Indexes", "created_at": now,
         "updated_at": now},
    )
    profile_id = insert_row(
        "profile_version",
        {"job_seeker_id": seeker_id, "version": 1, "sections": {}, "created_at": now},
    )
    directive_id = insert_row(
        "directive_set", {"job_seeker_id": seeker_id, "name": "default", "created_at": now}
    )
    return insert_row(
        "campaign",
        {"job_seeker_id": seeker_id, "directive_set_id": directive_id,
         "profile_version_id": profile_id, "name": "indexed", "status": "running",
         "created_at": now},
    )


def _plan(campaign_id: str) -> list[str]:
    return [row["detail"] for row in query_all("EXPLAIN QUERY PLAN " + PLAN_QUERY, (campaign_id,))]


def test_migration_152_adds_the_plan_index_and_records_itself(db: None) -> None:
    assert query_one(
        "SELECT name FROM sqlite_master WHERE type = 'index' "
        "AND name = 'idx_plan_campaign_created'"
    ) is not None
    assert query_one(
        "SELECT name FROM sqlite_master WHERE type = 'index' "
        "AND name = 'idx_vacancy_company_agg'"
    ) is not None
    # The migration is part of the numbered history, not a hand-run script.
    assert query_one("SELECT name FROM schema_migration WHERE version = '152'") is not None


def test_the_plan_query_searches_the_new_index_without_a_temp_btree(db: None) -> None:
    campaign_id = _campaign()
    for adapter_key in ("ats.first", "website.second", "registry.third"):
        insert_row(
            "source_plan_item",
            {"campaign_id": campaign_id, "adapter_key": adapter_key,
             "created_at": utcnow(), "status": "planned"},
        )

    plan = _plan(campaign_id)

    assert any("idx_plan_campaign_created" in detail for detail in plan), plan
    assert any(detail.startswith("SEARCH") for detail in plan), plan
    # The whole point of indexing ``created_at``: the sort is free.
    assert not any("TEMP B-TREE" in detail for detail in plan), plan
