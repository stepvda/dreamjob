"""The source catalogue tells the truth about what a run will do (FR-161, FR-185).

Three untruths were costing a campaign something real, and every one of them
was invisible on the dashboard because the row *looked* fine:

* ``broken_board`` and ``stub_board`` were test fixtures that leaked into the
  installed database.  Source selection (FR-164) cannot tell a row with no
  adapter from a real source, so it selected them and the campaign spent a plan
  item on each - two per campaign, for ever (plan item C5).
* the catalogue recorded ``rate_limit_rps = 1.0`` for the seven ATS hosts while
  ``DomainLimiter`` ran them at the global 0.5, and ``planning.estimate()``
  prices a plan from the catalogue: the duration the user approved was half the
  duration the run would take.  europa.eu's ``Crawl-delay: 10`` was missing
  from the row entirely, so the EURES bucket was priced at twenty times the
  throughput the site permits.
* ``board.indeed`` and ``board.stepstone`` carried legal notes reading
  "disabled by default" over a row whose ``enabled`` column said 1.

Everything here runs against a throw-away SQLite file, with no network: the
adapters are only imported and registered.

References: docs/Data_Gathering_Plan.md items C5, C6, C8 and section 6.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from dreamjob.adapters import load_all
from dreamjob.adapters.base import all_adapters, get_adapter
from dreamjob.config import REPO_ROOT, Settings, get_settings
from dreamjob.db.connection import upsert_row, utcnow
from dreamjob.db.migrator import migrate
from dreamjob.db.repositories import admin as admin_repo
from dreamjob.db.repositories import campaigns as campaign_repo
from dreamjob.pipeline import knowledge_base, planning

#: The rows the plan names are ``broken_board`` and ``stub_board``; both are
#: also live stub adapters in other unit modules, and an adapter that exists is
#: exactly what the prune must not touch.  These two keys have the same shape
#: and no adapter anywhere, which is the condition under test.
LEAKED = {"leaked_broken_board", "leaked_stub_board"}


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """A scratch database, at the compliant rate limits.

    The rates are pinned rather than inherited from the developer's ``.env``
    because they are what this file is about: 0.5 requests per second per
    domain, and a published ``Crawl-delay`` honoured on top of it (FR-182).
    """
    monkeypatch.setenv("DREAMJOB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DREAMJOB_DB_PATH", str(tmp_path / "data" / "test.db"))
    monkeypatch.setenv("DREAMJOB_PER_DOMAIN_RPS", "0.5")
    monkeypatch.setenv("DREAMJOB_HONOUR_CRAWL_DELAY", "true")
    get_settings.cache_clear()
    get_settings()
    migrate()
    yield
    get_settings.cache_clear()


def _iso(days_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat(timespec="seconds")


def _leak(adapter_key: str, **overrides) -> None:
    """Write the kind of row a test fixture left behind in the real database."""
    upsert_row(
        "source_catalogue",
        {
            "adapter_key": adapter_key,
            "display_name": adapter_key,
            "source_type": "job_board",
            "access_method": "http",
            "coverage_countries": [],
            "enabled": 1,
            "requires_ack": 0,
            "tos_status": "permitted",
            "rate_limit_rps": 0.5,
            "updated_at": utcnow(),
            **overrides,
        },
        ["adapter_key"],
    )


def _shipped_adapters() -> dict[str, type]:
    """The adapters that ship with the product, not the stubs other tests register."""
    return {
        key: cls
        for key, cls in all_adapters().items()
        if cls.__module__.startswith("dreamjob.adapters.")
    }


def _selected_for_belgium() -> set[str]:
    catalogue = campaign_repo.list_catalogue(enabled_only=True)
    return {
        entry["adapter_key"]
        for entry in planning.select_sources(catalogue, countries=["BE"]).selected
    }


# ---------------------------------------------------------------------------
# C5: a catalogue row with no adapter behind it
# ---------------------------------------------------------------------------


def test_a_leaked_test_row_is_selected_as_a_real_source_until_it_is_pruned(db):
    """FR-164 cannot see the difference; the catalogue has to not offer it.

    This is the defect made visible: with the fixture rows present, source
    selection picks them, and every campaign then plans two items against
    adapters that do not exist.
    """
    load_all()
    for key in LEAKED:
        _leak(key)

    assert LEAKED <= _selected_for_belgium(), "the defect: fixtures planned as real sources"

    pruned = admin_repo.prune_unknown_sources(all_adapters())

    assert set(pruned) == LEAKED
    assert not (LEAKED & _selected_for_belgium())
    for key in LEAKED:
        assert admin_repo.get_source(key) is None


def test_a_restart_removes_a_catalogue_row_whose_adapter_does_not_exist(db):
    """The prune is part of registration, so an installed leak clears itself."""
    load_all()
    _leak("leaked_stub_board")
    assert admin_repo.get_source("leaked_stub_board") is not None

    load_all()

    assert admin_repo.get_source("leaked_stub_board") is None
    # Every real adapter still has its row.
    catalogued = {r["adapter_key"] for r in admin_repo.list_sources()}
    assert set(all_adapters()) <= catalogued


def test_the_prune_leaves_a_row_an_administrator_decided_about(db):
    """A switched-off or acknowledged row is a decision, not an accident."""
    load_all()
    _leak("leaked_stub_board", enabled=0)
    _leak("leaked_broken_board", acknowledged_at=utcnow())

    assert admin_repo.prune_unknown_sources(all_adapters()) == []
    assert admin_repo.get_source("leaked_stub_board") is not None
    assert admin_repo.get_source("leaked_broken_board") is not None


def test_the_unit_suite_never_writes_to_the_installed_database():
    """C5: the leak was a test run with the real ``DREAMJOB_DB_PATH`` in place.

    ``sync_catalogue`` writes every adapter in the process-wide registry
    (NFR-601), and a full unit run has every module's stub adapters in that
    registry, so one ``load_all()`` against the installed database was all it
    took to catalogue ``stub_board`` as a real source.  ``tests/unit/conftest``
    points the whole suite somewhere harmless; this is the assertion that says
    so out loud if it is ever removed.
    """
    installed = (REPO_ROOT / "data" / "dreamjob.db").resolve()
    assert get_settings().abs_db_path.resolve() != installed


def test_nothing_is_pruned_when_no_adapter_could_be_loaded(db):
    """One broken import must not empty the catalogue: every row would look orphaned."""
    load_all()
    before = {r["adapter_key"] for r in admin_repo.list_sources()}
    assert before

    assert admin_repo.prune_unknown_sources([]) == []
    assert {r["adapter_key"] for r in admin_repo.list_sources()} == before


# ---------------------------------------------------------------------------
# The rate the catalogue promises is the rate the limiter applies
# ---------------------------------------------------------------------------


def test_the_catalogue_records_the_rate_the_limiter_will_apply(db):
    """The ATS rows said 1.0 rps; DomainLimiter has always run them at 0.5."""
    load_all()
    rows = {r["adapter_key"]: r for r in admin_repo.list_sources()}
    ats_keys = [key for key in _shipped_adapters() if key.startswith("ats.")]
    assert len(ats_keys) >= 7

    for key in ats_keys:
        assert get_adapter(key).rate_limit_rps == 1.0, f"{key} still asks for 1.0"
        assert rows[key]["rate_limit_rps"] == 0.5, f"{key} is catalogued faster than it runs"


def test_a_published_crawl_delay_lowers_the_catalogued_rate(db):
    """europa.eu asks for ten seconds; 0.5 rps would be twenty times that."""
    load_all()
    assert admin_repo.PUBLISHED_CRAWL_DELAY_SECONDS["board.eures"] == 10.0
    assert admin_repo.get_source("board.eures")["rate_limit_rps"] == 0.1


def test_a_source_that_asks_to_be_read_more_slowly_keeps_its_own_rate(db):
    """The reconciliation is a floor, never a raise: politeness is allowed."""
    load_all()
    assert admin_repo.effective_rate_limit_rps("board.jobat", 0.3) == 0.3
    assert admin_repo.get_source("board.jobat")["rate_limit_rps"] == 0.3


def test_the_plan_estimate_is_priced_at_the_rate_that_will_run(db):
    """FR-163: ``estimate()`` reads ``rate_limit_rps`` off the catalogue row.

    A 1,000-board Greenhouse bucket is 33 minutes of limiter time (plan
    section 2.4).  Priced at the catalogue's old 1.0 rps it read as half that,
    so a plan that fits inside NFR-103 on the review screen would have run over
    it.
    """
    load_all()
    entry = campaign_repo.get_catalogue_entry("ats.greenhouse")
    honest_seconds, _ = planning.estimate(entry, 1000)
    optimistic_seconds, _ = planning.estimate({**entry, "rate_limit_rps": 1.0}, 1000)

    assert honest_seconds >= 1000 * 2.0          # 2 s between requests at 0.5 rps
    assert honest_seconds > optimistic_seconds


def test_the_eures_bucket_is_priced_at_the_crawl_delay(db):
    """600 partitioned requests at 10 s is 100 minutes, the plan's long pole."""
    load_all()
    entry = campaign_repo.get_catalogue_entry("board.eures")
    seconds, _ = planning.estimate(entry, 600)
    assert seconds >= 600 * 10


# ---------------------------------------------------------------------------
# Section 6: the sources that stay off, and say why
# ---------------------------------------------------------------------------


def test_a_source_whose_notes_say_it_is_disabled_is_disabled(db):
    """IR-101: "disabled by default" printed over ``enabled = 1`` is a lie."""
    load_all()
    for key in admin_repo.FORBIDDEN_SOURCES:
        row = admin_repo.get_source(key)
        assert row is not None, key
        assert row["enabled"] == 0, f"{key} is catalogued as enabled"
        assert row["requires_ack"] == 1, key
        assert row["legal_notes"], f"{key} states no reason"
        assert get_adapter(key).is_enabled() is False, key

    selection = planning.select_sources(
        campaign_repo.list_catalogue(enabled_only=False), countries=["BE"]
    )
    rejected = {r["adapter_key"]: r["reason"] for r in selection.rejected}
    assert set(admin_repo.FORBIDDEN_SOURCES) <= set(rejected)
    assert not (set(admin_repo.FORBIDDEN_SOURCES) & {e["adapter_key"] for e in selection.selected})


def test_an_acknowledged_prohibited_source_stays_the_administrators_decision(db):
    """FR-363: a licence is the administrator's to hold, and a restart must not undo it."""
    load_all()
    admin_repo.update_source("board.indeed", {"acknowledged_at": utcnow(), "enabled": 1})

    load_all()

    row = admin_repo.get_source("board.indeed")
    assert row["enabled"] == 1
    assert row["acknowledged_at"]


def test_every_catalogued_source_states_a_position_that_matches_its_row(db):
    """FR-185: walk the whole catalogue, not the adapters one believes in."""
    load_all()
    rows = {r["adapter_key"]: r for r in admin_repo.list_sources()}
    shipped = _shipped_adapters()
    assert len(shipped) >= 24
    assert set(shipped) <= set(rows)

    for key in shipped:
        row = rows[key]
        ceiling = admin_repo.effective_rate_limit_rps(key)
        assert row["legal_notes"], f"{key} states no legal position"
        assert row["tos_status"] in {"permitted", "restricted", "prohibited"}, key
        assert row["rate_limit_rps"] is not None, key
        assert row["rate_limit_rps"] <= ceiling + 1e-9, f"{key} promises more than it gets"
        if row["tos_status"] == "prohibited" and not row["acknowledged_at"]:
            assert row["requires_ack"] == 1, key
            assert not row["enabled"], key


# ---------------------------------------------------------------------------
# C8: board-registry liveness decays
# ---------------------------------------------------------------------------


def test_a_board_registry_entry_goes_stale_after_a_month(db):
    """FR-343: 87.5% of current slugs answer, against 27.5% of stale ones."""
    assert knowledge_base.DEFAULT_STALENESS_DAYS["board_registry"] == 30
    assert knowledge_base.get_staleness_policy()["board_registry"] == 30
    assert knowledge_base.is_stale("board_registry", _iso(31)) is True
    assert knowledge_base.is_stale("board_registry", _iso(29)) is False


def test_the_board_registry_window_is_one_the_administrator_can_change(db):
    """FR-343 is a policy, not a constant.

    ``set_staleness_policy`` refuses a record type it does not know, so before
    the key existed an administrator asking for monthly re-verification got
    ``ValueError: Unknown record type 'board_registry'`` - and the FR-343
    screen never offered the row at all.
    """
    assert "board_registry" in knowledge_base.DEFAULT_STALENESS_DAYS
    policy = knowledge_base.set_staleness_policy({"board_registry": 14})
    assert policy["board_registry"] == 14
    assert knowledge_base.get_staleness_policy()["board_registry"] == 14
    assert knowledge_base.is_stale("board_registry", _iso(20)) is True


# ---------------------------------------------------------------------------
# The example environment is the operator's copy of the same constants
# ---------------------------------------------------------------------------


def test_the_example_environment_keeps_the_compliant_rate_limits():
    """C9: restated so that nobody "fixes" them upward (plan section 7)."""
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    for line in (
        "DREAMJOB_PER_DOMAIN_RPS=0.5",
        "DREAMJOB_HTTP_MAX_CONCURRENCY=20",
        "DREAMJOB_HTTP_CACHE_TTL_SECONDS=86400",
        "DREAMJOB_HONOUR_CRAWL_DELAY=true",
    ):
        assert line in text, f"{line} is missing from .env.example"


def test_every_setting_is_documented_in_the_example_environment():
    """A setting nobody can find is a setting nobody sets (CR-407)."""
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    documented = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", text, re.MULTILINE))
    aliases = {
        field.alias for field in Settings.model_fields.values() if isinstance(field.alias, str)
    }
    assert aliases <= documented, f"undocumented: {sorted(aliases - documented)}"
