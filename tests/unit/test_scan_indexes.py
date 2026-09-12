"""The scan indexes added by migration 153 (NFR-101, NFR-102).

Two lookups ran without a usable index:

* ``contact`` is read with ``WHERE lower(email) = ?`` while
  ``idx_contact_email`` indexes the raw column, so the address lookup during
  discovery scanned the table;
* the profile view reads one entity's provenance and orders it by
  ``field_path``, which no existing index provides - the planner used
  ``idx_prov_entity_plan`` and sorted in a temporary B-tree.

The ``EXPLAIN QUERY PLAN`` half is the only honest check: a query that is
correct and fast on a three-row fixture says nothing about the fifty-thousand-
row plan a live corpus gets.  Assert the plan SQLite reports, not a stopwatch.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from dreamjob.config import get_settings
from dreamjob.db.connection import query_all, query_one
from dreamjob.db.migrator import migrate

CONTACT_QUERY = "SELECT id, email FROM contact WHERE lower(email) = ? LIMIT 1"

PROVENANCE_FIELD_QUERY = (
    "SELECT p.field_path, p.confidence, p.adapter_key, p.created_at "
    "FROM provenance p LEFT JOIN raw_document d ON d.id = p.raw_document_id "
    "WHERE p.entity_type = 'company' AND p.entity_id = ? AND p.field_path IS NOT NULL "
    "AND p.field_path NOT LIKE ? ORDER BY p.field_path"
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


def _plan(sql: str, params: tuple) -> list[str]:
    return [row["detail"] for row in query_all("EXPLAIN QUERY PLAN " + sql, params)]


def test_migration_153_adds_the_scan_indexes_and_records_itself(db: None) -> None:
    for name in ("idx_contact_email_lower", "idx_prov_entity_field"):
        assert query_one(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name = ?", (name,)
        ) is not None, f"migration 153 did not create {name}"
    # The migration is part of the numbered history, not a hand-run script.
    assert query_one("SELECT name FROM schema_migration WHERE version = '153'") is not None


def test_migration_153_contact_email_lookup_seeks_the_expression_index(db: None) -> None:
    plan = _plan(CONTACT_QUERY, ("someone@example.org",))

    assert any("idx_contact_email_lower" in detail for detail in plan), plan
    assert not any(detail.startswith("SCAN") for detail in plan), plan


def test_migration_153_provenance_field_lookup_drops_the_temp_btree(db: None) -> None:
    plan = _plan(PROVENANCE_FIELD_QUERY, ("company-id", "crawl:%"))

    assert any("idx_prov_entity_field" in detail for detail in plan), plan
    assert not any("TEMP B-TREE" in detail for detail in plan), (
        f"the field_path ordering is still sorted outside the index: {plan}"
    )
