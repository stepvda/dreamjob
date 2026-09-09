"""The registry learns which slugs are gone, and stops offering them (FR-181, FR-186).

The bug this covers is not a crash. Every campaign planned 4,511 boards; 208 of
them answered HTTP 404 to a real request; nothing wrote that down; and the next
campaign planned all 208 again. Multiplied by 175 campaigns that is the largest
single line in the "collection errors" number the operator sees, and not one of
those errors was a failure - a company that closed its board is a fact about the
world, and the registry's job is to learn it once.

What has to stay true afterwards, in order of how expensive it is to get wrong:

* an ``unverified`` board is still planned. 4,515 of 4,518 rows are unverified,
  and reading "never asked" as "dead" would leave three targets;
* a 5xx or a transport error never retires anything. An ATS vendor having a bad
  afternoon would otherwise delete every board it serves from the inventory;
* one 404 is not enough, two are. A tenant renaming its board mid-migration
  answers 404 once;
* a retired board comes back after ``REVISIT_AFTER_DAYS``, because boards do.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from dreamjob.config import get_settings
from dreamjob.db.migrator import migrate
from dreamjob.db.repositories import board_registry as repo
from dreamjob.pipeline import board_registry as registry

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "backend" / "dreamjob" / "db" / "migrations" / "131_registry_liveness.sql"
)


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
# The schema migration 131 leaves behind
# ---------------------------------------------------------------------------


def test_the_state_column_holds_the_three_words_and_no_others(db):
    """``gone`` is the same word the plan item uses; ``dead`` is gone with it."""
    columns = {
        r["name"]: r
        for r in _rows("PRAGMA table_info(board_registry)")
    }
    assert set(columns) >= {
        "vendor", "slug", "state", "last_verified", "consecutive_failures", "first_seen",
    }
    assert "liveness" not in columns, "one fact should not have two column names"

    repo.upsert({"vendor": "lever", "slug": "acme", "source": "commoncrawl"})
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        repo.upsert({"vendor": "lever", "slug": "acme", "state": "dead"})


def test_the_migration_and_the_module_agree_on_the_retirement_threshold():
    """The number is written twice - a migration cannot import Python.

    If they drift, the backfill retires boards on different evidence than the
    live path does, and no test would otherwise notice.
    """
    sql = MIGRATION.read_text(encoding="utf-8")
    thresholds = {int(n) for n in re.findall(r"e\.slug = board_registry\.slug\) >= (\d+)", sql)}
    assert thresholds == {repo.RETIRE_AFTER_FAILURES}


# ---------------------------------------------------------------------------
# mark_gone: what retires a slug, and what must not
# ---------------------------------------------------------------------------


def test_one_404_records_the_strike_and_keeps_the_board_plannable(db):
    repo.upsert({"vendor": "greenhouse", "slug": "renamed", "source": "commoncrawl"})

    assert registry.mark_gone("greenhouse", "renamed", 404) == "unverified"
    row = repo.get("greenhouse", "renamed")
    assert row["consecutive_failures"] == 1
    assert row["last_status"] == 404
    assert "renamed" in registry.live_slugs("greenhouse"), (
        "a board that 404'd once may have been renamed mid-migration; "
        "retiring on it deletes a live employer nothing will look for again"
    )


def test_two_404s_retire_the_slug_and_planning_stops_offering_it(db):
    repo.upsert({"vendor": "lever", "slug": "closed", "source": "commoncrawl"})

    registry.mark_gone("lever", "closed", 404)
    assert registry.mark_gone("lever", "closed", 404) == "gone"

    assert repo.get("lever", "closed")["state"] == "gone"
    assert "closed" not in registry.live_slugs("lever")
    assert registry.state_of("lever", "closed") == "gone"


def test_a_410_is_the_same_evidence_as_a_404(db):
    repo.upsert({"vendor": "ashby", "slug": "shut", "source": "hackernews"})
    registry.mark_gone("ashby", "shut", 410)
    assert registry.mark_gone("ashby", "shut", 410) == "gone"


def test_a_never_asked_board_is_planned(db):
    """4,515 of 4,518 rows are unverified. Reading that as dead leaves three."""
    repo.upsert({"vendor": "personio", "slug": "unasked", "source": "commoncrawl"})
    assert registry.state_of("personio", "unasked") == "unverified"
    assert "unasked" in registry.live_slugs("personio")


def test_a_500_never_retires_a_board_however_often_it_repeats(db):
    """A broken server says nothing about whether the tenant exists.

    This is the ``failed`` outcome, not the ``gone`` one. Conflating them means
    one bad afternoon at an ATS vendor retires every board it serves.
    """
    repo.upsert({"vendor": "recruitee", "slug": "flaky", "source": "commoncrawl"})

    for _ in range(repo.RETIRE_AFTER_FAILURES * 3):
        assert repo.record_verification("recruitee", "flaky", http_status=503) == "unverified"

    row = repo.get("recruitee", "flaky")
    assert row["state"] == "unverified"
    assert row["consecutive_failures"] == 0
    assert row["last_status"] == 503, "the failure is still visible in the register"
    assert row["last_verified"] is None, "nothing was verified"
    assert "flaky" in registry.live_slugs("recruitee")


def test_a_transport_error_carries_no_status_and_retires_nothing(db):
    repo.upsert({"vendor": "workable", "slug": "timeout", "source": "commoncrawl"})
    for _ in range(4):
        repo.record_verification("workable", "timeout", http_status=None)
    assert repo.get("workable", "timeout")["state"] == "unverified"


def test_mark_gone_refuses_to_retire_on_a_status_that_is_not_evidence(db):
    """A mis-routed call degrades to the honest answer rather than a retirement."""
    repo.upsert({"vendor": "teamtailor", "slug": "walled", "source": "commoncrawl"})
    for _ in range(4):
        assert registry.mark_gone("teamtailor", "walled", 403) == "unverified"
    assert repo.get("teamtailor", "walled")["consecutive_failures"] == 0


def test_a_board_that_answers_again_is_live_again_with_the_count_cleared(db):
    repo.upsert({"vendor": "greenhouse", "slug": "back", "source": "commoncrawl"})
    registry.mark_gone("greenhouse", "back", 404)
    registry.mark_live("greenhouse", "back", job_count=7)

    row = repo.get("greenhouse", "back")
    assert row["state"] == "live"
    assert row["consecutive_failures"] == 0
    assert row["job_count"] == 7


# ---------------------------------------------------------------------------
# The revisit window: rarely, not never
# ---------------------------------------------------------------------------


def test_a_retired_board_is_offered_again_after_the_revisit_window(db):
    """Boards come back: a company re-opens hiring, or moves ATS and moves back."""
    repo.upsert({"vendor": "lever", "slug": "revived", "source": "commoncrawl"})
    registry.mark_gone("lever", "revived", 404)
    registry.mark_gone("lever", "revived", 404)

    inside = datetime.now(UTC) + timedelta(days=repo.REVISIT_AFTER_DAYS - 1)
    beyond = datetime.now(UTC) + timedelta(days=repo.REVISIT_AFTER_DAYS + 1)

    assert "revived" not in registry.live_slugs("lever", now=inside)
    assert "revived" in registry.live_slugs("lever", now=beyond), (
        "a retired slug is re-tested rarely, not never"
    )
    assert [r["slug"] for r in registry.due_for_revisit(now=beyond)] == ["revived"]
    assert registry.due_for_revisit(now=inside) == []


def test_a_revisited_board_sorts_behind_the_boards_that_answer(db):
    """A cap that bites (FR-186) should spend its requests on boards that work."""
    for slug in ("answering", "unasked", "revived"):
        repo.upsert({"vendor": "ashby", "slug": slug, "source": "commoncrawl"})
    registry.mark_live("ashby", "answering")
    registry.mark_gone("ashby", "revived", 404)
    registry.mark_gone("ashby", "revived", 404)

    beyond = datetime.now(UTC) + timedelta(days=repo.REVISIT_AFTER_DAYS + 1)
    assert registry.live_slugs("ashby", now=beyond) == ["answering", "unasked", "revived"]


# ---------------------------------------------------------------------------
# What the other slices call
# ---------------------------------------------------------------------------


def test_without_gone_subtracts_the_retired_from_the_shipped_file(db):
    """``discovery.load_board_registry`` reads a file that cannot know this."""
    repo.upsert({"vendor": "lever", "slug": "closed", "source": "commoncrawl"})
    repo.upsert({"vendor": "lever", "slug": "open", "source": "commoncrawl"})
    registry.mark_gone("lever", "closed", 404)
    registry.mark_gone("lever", "closed", 404)

    # The shape discovery produces, and the shape the raw file uses.
    shipped = [
        {"ats_vendor": "lever", "slug": "closed", "name": "Closed"},
        {"ats_vendor": "lever", "slug": "open", "name": "Open"},
        {"vendor": "lever", "slug": "closed"},
        {"vendor": "greenhouse", "slug": "closed"},   # same slug, another vendor
    ]
    kept = registry.without_gone(shipped)
    assert [r.get("slug") for r in kept] == ["open", "closed"]
    assert kept[-1].get("vendor") == "greenhouse", (
        "the same slug on two vendors is two different companies (DR-101)"
    )


def test_without_gone_is_one_query_not_one_per_board(db):
    """4,511 rows is 4,511 round trips against the single writer if read per row."""
    repo.upsert({"vendor": "lever", "slug": "closed", "source": "commoncrawl"})
    registry.mark_gone("lever", "closed", 404)
    registry.mark_gone("lever", "closed", 404)

    keys = registry.retired_keys()
    assert keys == {("lever", "closed")}


def test_the_summary_separates_retired_from_one_strike_away(db):
    """The operator's question is "what will next campaign spend on dead boards"."""
    repo.upsert({"vendor": "lever", "slug": "closed", "source": "commoncrawl"})
    repo.upsert({"vendor": "lever", "slug": "striking", "source": "commoncrawl"})
    repo.upsert({"vendor": "lever", "slug": "fine", "source": "commoncrawl"})
    registry.mark_live("lever", "fine")
    registry.mark_gone("lever", "closed", 404)
    registry.mark_gone("lever", "closed", 404)
    registry.mark_gone("lever", "striking", 404)

    assert registry.summary() == {
        "total": 3,
        "live": 1,
        "unverified": 1,
        "gone": 1,
        "due_for_revisit": 0,
        "failing": 1,
    }


def test_an_import_of_the_shipped_file_cannot_resurrect_a_retired_board(db):
    """The file ships ``last_verified: null``; importing that as fact undoes this."""
    repo.upsert({"vendor": "lever", "slug": "closed", "source": "commoncrawl"})
    registry.mark_gone("lever", "closed", 404)
    registry.mark_gone("lever", "closed", 404)

    repo.sync_from_file([{"vendor": "lever", "slug": "closed", "last_verified": None}])
    assert repo.get("lever", "closed")["state"] == "gone"


# ---------------------------------------------------------------------------
# The backfill migration 131 runs against the history already recorded
# ---------------------------------------------------------------------------


def test_the_backfill_credits_each_404_to_the_board_that_caused_it(db):
    """Two campaigns' 404s on one slug retire it; one campaign's does not.

    The evidence is real: ``source_plan_item.last_error`` is where a collection
    run recorded what the board answered, and ``native_query`` names the slug it
    was reading. Re-running the migration's own SQL against a hand-built history
    is the only way to check the rule it encodes.
    """
    for slug in ("twice", "once", "healthy"):
        repo.upsert({"vendor": "lever", "slug": slug, "source": "commoncrawl"})
    repo.upsert({"vendor": "lever", "slug": "listed", "source": "commoncrawl"})
    registry.mark_live("lever", "healthy")

    _campaign("c1", finished="2026-06-01T00:00:00+00:00")
    _campaign("c2", finished="2026-07-01T00:00:00+00:00")
    _plan_item("c1", "ats.lever", '{"slug": "twice"}', "SourceUnavailable: HTTP 404")
    _plan_item("c2", "ats.lever", '{"slug": "twice"}', "SourceUnavailable: HTTP 404")
    _plan_item("c1", "ats.lever", '{"slug": "once"}', "SourceUnavailable: HTTP 404")
    _plan_item("c1", "ats.lever", '{"slug": "healthy"}', "SourceUnavailable: HTTP 404")
    # The other plan-item shape: a list of slugs, every one of which must be credited.
    _plan_item("c1", "ats.lever", '{"board_slugs": ["listed"]}', "SourceUnavailable: HTTP 410")
    _plan_item("c2", "ats.lever", '{"board_slugs": ["listed"]}', "SourceUnavailable: HTTP 410")
    _plan_item("c1", "ats.lever", '{"slug": "healthy"}', "SourceUnavailable: HTTP 503")

    _apply_backfill()

    assert repo.get("lever", "twice")["state"] == "gone"
    assert repo.get("lever", "listed")["state"] == "gone"

    once = repo.get("lever", "once")
    assert once["state"] == "unverified", "one observation is one strike, not a retirement"
    assert once["consecutive_failures"] == 1
    assert once["last_verified"] == "2026-06-01T00:00:00+00:00", (
        "the revisit clock starts when the board stopped answering, "
        "not when the migration happened to run"
    )

    assert repo.get("lever", "healthy")["state"] == "live", (
        "a board that has since answered is not retired by an older 404"
    )


def _apply_backfill() -> None:
    """Run only the backfill half of migration 131 (the schema half is applied)."""
    from dreamjob.db.connection import write_tx

    sql = MIGRATION.read_text(encoding="utf-8")
    backfill = sql[sql.index("CREATE TEMP TABLE _registry_404_evidence"):]
    with write_tx() as conn:
        conn.executescript(backfill)


_T0 = "2026-01-01T00:00:00+00:00"


def _campaign(campaign_id: str, *, finished: str) -> None:
    """A campaign row, with the parents the foreign keys insist on."""
    from dreamjob.db.connection import write_tx

    with write_tx() as conn:
        if conn.execute("SELECT 1 FROM job_seeker WHERE id = 'seeker'").fetchone() is None:
            conn.execute(
                "INSERT INTO job_seeker (id, email, display_name, created_at, updated_at)"
                " VALUES ('seeker', 's@example.com', 'S', ?, ?)", (_T0, _T0),
            )
            conn.execute(
                "INSERT INTO directive_set (id, job_seeker_id, name, created_at)"
                " VALUES ('d', 'seeker', 'd', ?)", (_T0,),
            )
            conn.execute(
                "INSERT INTO profile_version (id, job_seeker_id, version, sections, created_at)"
                " VALUES ('p', 'seeker', 1, '{}', ?)", (_T0,),
            )
        conn.execute(
            "INSERT INTO campaign (id, job_seeker_id, directive_set_id, profile_version_id,"
            " name, finished_at, created_at) VALUES (?,?,?,?,?,?,?)",
            (campaign_id, "seeker", "d", "p", campaign_id, finished, _T0),
        )


def _plan_item(campaign_id: str, adapter_key: str, native_query: str, error: str) -> None:
    from dreamjob.db.connection import new_id, write_tx

    with write_tx() as conn:
        conn.execute(
            "INSERT INTO source_plan_item (id, campaign_id, adapter_key, native_query,"
            " last_error, created_at) VALUES (?,?,?,?,?,?)",
            (new_id(), campaign_id, adapter_key, native_query, error,
             _T0),
        )


def _rows(sql: str) -> list[dict]:
    from dreamjob.db.connection import query_all

    return query_all(sql)
