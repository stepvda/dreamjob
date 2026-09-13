"""The endless background data-collection cycle (FR-161..166, FR-185, FR-281, FR-301..303).

What these tests pin down is the *state machine*: the switch persists, the
interval gates a tick, each phase is gated only by the jobs it would stack on
(so a running collection no longer starves ``score``, ``contacts`` or
``enrich``), the four phases rotate, a deferred phase records why and is not
stamped, and a phase that fails is recorded without stopping the loop.  Each
phase's own entry point is replaced with a recorder, so the tests assert the
call, the campaign and the bounded limits - never the network or the model.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from dreamjob.db.connection import execute, from_json, insert_row, query_one, utcnow
from dreamjob.db.repositories import admin as admin_repo
from dreamjob.db.repositories import campaigns as campaign_repo
from dreamjob.db.repositories import seekers as seeker_repo
from dreamjob.jobs.runner import QUEUED_ERROR_MARKER, runner
from dreamjob.pipeline import autopilot, continuous


@pytest.fixture(autouse=True)
def _clean_state() -> None:
    """The engine's settings are process-global; every test starts blank."""
    def clear() -> None:
        for key in admin_repo.settings_with_prefix("continuous."):
            admin_repo.delete_setting(key)

    clear()
    yield
    clear()


@pytest.fixture(autouse=True)
def _no_guarded_jobs() -> None:
    """Guards read the shared scratch database: no test inherits another's jobs.

    Without this, a job row a previous module left ``running`` or ``pending``
    would silently defer the phase under test.
    """
    kinds = tuple({k for ks in continuous.PHASE_JOB_KINDS.values() for k in ks})
    marks = ", ".join("?" for _ in kinds)

    def clear() -> None:
        execute(f"DELETE FROM job_run WHERE kind IN ({marks})", kinds)

    clear()
    yield
    clear()


@pytest.fixture()
def running_job() -> Callable[[str, str], str]:
    """Insert a job row in a guardable state and return its kind-checked id.

    A ``pending`` row carries the runner's queued marker, which is what the
    gate reads: a bare pending row is an orphan, not work in flight.
    """
    def add(kind: str, status: str = "running") -> str:
        row: dict = {"kind": kind, "status": status, "created_at": utcnow()}
        if status == "pending":
            row["last_error"] = QUEUED_ERROR_MARKER
        return insert_row("job_run", row)

    return add


@pytest.fixture()
def phases(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace every phase with a recorder, so order is observable."""
    calls: list[str] = []

    def recorder(name: str):
        async def run() -> dict:
            calls.append(name)
            return {f"{name}_count": len(calls)}

        return run

    for name in continuous.PHASES:
        monkeypatch.setattr(continuous, f"_phase_{name}", recorder(name))
    return calls


@pytest.fixture()
def ready_seeker() -> str:
    """A seeker autopilot's preflight accepts: profile written, consent given."""
    from dreamjob.security import auth_service as auth

    seeker_id = seeker_repo.create_seeker(
        f"continuous-{uuid4().hex[:8]}@example.com",
        "Continuous Tester",
        password_hash="x",
    )
    insert_row(
        "profile_version",
        {
            "job_seeker_id": seeker_id,
            "version": 1,
            "sections": {"summary": "Engineer"},
            "dream_job_statement": "Hands-on software design",
            "created_at": utcnow(),
        },
    )
    auth.record_consent(seeker_id, "llm_transfer", True)
    return seeker_id


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def test_toggle_persists_and_status_reflects_it() -> None:
    state = continuous.set_enabled(True, interval_seconds=600)
    assert state["enabled"] is True
    assert state["interval_seconds"] == 600

    stored = continuous.get_state()
    assert stored["enabled"] is True
    assert stored["interval_seconds"] == 600
    assert stored["phase"] == "discover"
    for key in (
        "seeker_cursor",
        "last_tick",
        "next_due",
        "last_report",
        "last_attempt",
        "scheduler",
        "counts",
    ):
        assert key in stored

    state = continuous.set_enabled(False)
    assert state["enabled"] is False
    assert continuous.get_state()["enabled"] is False
    # Disabling does not lose the interval an operator configured.
    assert continuous.get_state()["interval_seconds"] == 600


def test_the_interval_is_bounded() -> None:
    with pytest.raises(ValueError):
        continuous.set_enabled(True, interval_seconds=10)
    with pytest.raises(ValueError):
        continuous.set_enabled(True, interval_seconds=86401)
    assert continuous.get_state()["interval_seconds"] == continuous.DEFAULT_INTERVAL_SECONDS


# ---------------------------------------------------------------------------
# The tick
# ---------------------------------------------------------------------------


def test_a_disabled_engine_runs_nothing(phases: list[str]) -> None:
    continuous.set_enabled(False)
    assert asyncio.run(continuous.run_phase()) == {"enabled": False}
    assert phases == []


def test_the_phases_rotate_round_robin(phases: list[str]) -> None:
    continuous.set_enabled(True, interval_seconds=300)
    seen = [
        asyncio.run(continuous.run_phase(force=True))["phase"] for _ in range(5)
    ]
    assert seen == ["discover", "contacts", "enrich", "score", "discover"]
    assert continuous.get_state()["phase"] == "contacts", "the cursor advanced one step"


def test_interval_gating_and_force(phases: list[str]) -> None:
    continuous.set_enabled(True, interval_seconds=3600)
    admin_repo.set_setting(continuous.SETTING_LAST_PHASE_AT, utcnow())

    assert asyncio.run(continuous.run_phase()) == {"skipped": "not due"}
    assert continuous.get_state()["last_attempt"]["reason"] == "not due"
    assert phases == []

    old = (datetime.now(UTC) - timedelta(seconds=7200)).isoformat(timespec="seconds")
    admin_repo.set_setting(continuous.SETTING_LAST_PHASE_AT, old)
    assert asyncio.run(continuous.run_phase())["phase"] == "discover"
    assert phases == ["discover"]

    # force ignores the gate entirely.
    admin_repo.set_setting(continuous.SETTING_LAST_PHASE_AT, utcnow())
    assert asyncio.run(continuous.run_phase(force=True))["phase"] == "contacts"
    assert phases == ["discover", "contacts"]


@pytest.mark.parametrize("status", ["running", "pending"])
def test_an_autopilot_defers_discover_even_when_forced(
    status: str, running_job: Callable[[str, str], str], phases: list[str]
) -> None:
    """The phase that would start the same chain gives way to it."""
    continuous.set_enabled(True)
    running_job("autopilot", status)

    report = asyncio.run(continuous.run_phase(force=True, phase="discover"))

    assert report == {"deferred": "autopilot job running", "blocked_by": "autopilot"}
    assert phases == []
    state = continuous.get_state()
    assert state["last_phase_at"] is None, "a deferred phase must not be stamped as run"
    assert state["last_attempt"]["phase"] == "discover"
    assert state["last_attempt"]["reason"] == "autopilot job running"


def test_an_orphan_pending_job_does_not_freeze_the_loop(phases: list[str]) -> None:
    """A bare pending row is a crash's leftover, not a job holding resources."""
    continuous.set_enabled(True)
    insert_row("job_run", {"kind": "autopilot", "status": "pending", "created_at": utcnow()})

    report = asyncio.run(continuous.run_phase(force=True, phase="discover"))

    assert report["phase"] == "discover"
    assert phases == ["discover"]


@pytest.mark.parametrize("kind", ["company_enrichment", "financial"])
def test_an_enrichment_job_defers_only_enrich(
    kind: str, running_job: Callable[[str, str], str], phases: list[str]
) -> None:
    continuous.set_enabled(True)
    running_job(kind)

    for phase in ("discover", "contacts", "score"):
        assert asyncio.run(continuous.run_phase(force=True, phase=phase))["phase"] == phase
    deferred = asyncio.run(continuous.run_phase(force=True, phase="enrich"))

    assert deferred == {"deferred": f"{kind} job running", "blocked_by": kind}
    assert phases == ["discover", "contacts", "score"]


@pytest.mark.parametrize("phase", ["discover", "contacts", "enrich", "score"])
def test_a_running_collection_does_not_block_the_other_phases(
    phase: str, running_job: Callable[[str, str], str], phases: list[str]
) -> None:
    """A running collection owns its own resources; every phase may run beside it.

    ``discover`` is included on purpose: its own autopilot chain is gated, but
    an unrelated manual collection must not starve the loop - the continuous
    run is bounded to a few dozen pages, so it can share the pool.
    """
    continuous.set_enabled(True)
    running_job("collection")

    report = asyncio.run(continuous.run_phase(force=True, phase=phase))

    assert report["phase"] == phase
    assert "deferred" not in report
    assert phases == [phase]


def test_a_deferral_is_an_attempt_not_a_run(phases: list[str]) -> None:
    """The admin reads ``last_attempt`` to see why an idle cycle is idle."""
    continuous.set_enabled(True)
    insert_row("job_run", {"kind": "autopilot", "status": "running", "created_at": utcnow()})

    report = asyncio.run(continuous.run_phase(force=True))
    assert report["deferred"] == "autopilot job running"

    state = continuous.get_state()
    assert state["last_attempt"]["reason"] == "autopilot job running"
    assert state["last_phase_at"] is None
    assert state["last_report"] is None


def test_a_real_phase_stamps_the_run_and_the_report(phases: list[str]) -> None:
    continuous.set_enabled(True)

    report = asyncio.run(continuous.run_phase(force=True, phase="score"))

    state = continuous.get_state()
    assert state["last_phase_at"] == report["at"]
    assert state["last_report"]["phase"] == "score"
    assert state["last_report"]["score_count"] == 1, "the report keeps the phase counters"
    assert state["next_due"] is not None


def test_a_failing_phase_is_recorded_and_the_cycle_continues(
    monkeypatch: pytest.MonkeyPatch, phases: list[str]
) -> None:
    continuous.set_enabled(True)

    async def boom() -> dict:
        raise RuntimeError("registry unreachable")

    monkeypatch.setattr(continuous, "_phase_discover", boom)
    first = asyncio.run(continuous.run_phase(force=True))
    assert first["phase"] == "discover"
    assert "registry unreachable" in first["error"]

    second = asyncio.run(continuous.run_phase(force=True))
    assert second["phase"] == "contacts", "the cycle did not advance after a failure"
    assert phases == ["contacts"]


def test_a_manual_phase_override_does_not_move_the_cycle(phases: list[str]) -> None:
    continuous.set_enabled(True)
    admin_repo.set_setting(continuous.SETTING_PHASE, "enrich")

    report = asyncio.run(continuous.run_phase(force=True, phase="score"))
    assert report["phase"] == "score"
    assert phases == ["score"]
    assert continuous.get_state()["phase"] == "enrich", (
        "a manual look at one phase must not make the next tick skip it"
    )


# ---------------------------------------------------------------------------
# discover
# ---------------------------------------------------------------------------


def test_discover_starts_autopilot_on_the_continuous_campaign(
    monkeypatch: pytest.MonkeyPatch, ready_seeker: str
) -> None:
    started: dict = {}

    async def fake_start(seeker_id, *, options=None, campaign_id=None):
        started.update(seeker_id=seeker_id, campaign_id=campaign_id, options=options)
        return "autopilot-job"

    monkeypatch.setattr(continuous.autopilot, "start", fake_start)
    monkeypatch.setattr(continuous, "_seeker_pool", lambda: [ready_seeker])

    report = asyncio.run(continuous.run_phase(force=True, phase="discover"))

    assert report["seeker_id"] == ready_seeker
    assert report["job_id"] == "autopilot-job"
    assert started["seeker_id"] == ready_seeker
    assert started["campaign_id"] == report["campaign_id"]
    campaign = campaign_repo.get_campaign_any(report["campaign_id"])
    assert campaign["name"] == campaign_repo.CONTINUOUS_CAMPAIGN_NAME
    assert campaign["job_seeker_id"] == ready_seeker
    assert (
        admin_repo.get_setting(f"{continuous.SETTING_CAMPAIGN_PREFIX}{ready_seeker}")
        == report["campaign_id"]
    )


def test_discover_bounds_the_run_and_the_campaign(
    monkeypatch: pytest.MonkeyPatch, ready_seeker: str
) -> None:
    """A phase must finish in minutes, not hours (FR-186, NFR-102)."""
    started: dict = {}

    async def fake_start(seeker_id, *, options=None, campaign_id=None):
        started.update(options=options, campaign_id=campaign_id)
        return "autopilot-job"

    monkeypatch.setattr(continuous.autopilot, "start", fake_start)
    monkeypatch.setattr(continuous, "_seeker_pool", lambda: [ready_seeker])

    asyncio.run(continuous.run_phase(force=True, phase="discover"))

    assert started["options"] == {
        "max_pages": 60,
        "max_pages_per_source": 5,
        "max_companies": 10,
        "max_duration_seconds": 15 * 60,
        "company_limit": continuous.CONTINUOUS_COMPANY_LIMIT,
    }
    caps = campaign_repo.get_campaign_any(started["campaign_id"])["caps"]
    assert caps["max_pages"] == 60, "the plan and collection read the campaign's caps"
    assert caps["max_companies"] == 10
    assert caps["max_duration_seconds"] == 15 * 60


def test_autopilot_start_accepts_the_bounded_option_dict(
    monkeypatch: pytest.MonkeyPatch, ready_seeker: str
) -> None:
    """The phase passes a dict; autopilot normalises and persists it."""
    async def fake_start(job_id, worker=None):
        return None

    monkeypatch.setattr(runner, "start", fake_start)

    job_id = asyncio.run(
        autopilot.start(ready_seeker, options=continuous._discover_options())
    )

    row = query_one("SELECT checkpoint FROM job_run WHERE id = ?", (job_id,))
    options = (from_json(row["checkpoint"], {}) or {})["options"]
    assert options["max_pages"] == 60
    assert options["max_pages_per_source"] == 5
    assert options["max_duration_seconds"] == 15 * 60
    assert options["company_limit"] == continuous.CONTINUOUS_COMPANY_LIMIT


def test_a_seeker_without_a_profile_is_skipped_with_a_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    not_ready = seeker_repo.create_seeker(
        f"continuous-{uuid4().hex[:8]}@example.com", "No Profile", password_hash="x"
    )
    started: list[str] = []

    async def fake_start(seeker_id, *, options=None, campaign_id=None):
        started.append(seeker_id)
        return "job"

    monkeypatch.setattr(continuous.autopilot, "start", fake_start)
    monkeypatch.setattr(continuous, "_seeker_pool", lambda: [not_ready])

    report = asyncio.run(continuous.run_phase(force=True, phase="discover"))

    assert started == [], "autopilot was started for a seeker with no profile"
    assert report["skipped"] == "no ready seeker"
    assert report["reasons"][0]["seeker_id"] == not_ready
    assert "no_profile" in report["reasons"][0]["reasons"]


def test_the_continuous_campaign_is_reused_not_recreated(ready_seeker: str) -> None:
    first = campaign_repo.get_or_create_continuous_campaign(ready_seeker)
    assert first
    assert campaign_repo.get_or_create_continuous_campaign(ready_seeker) == first

    campaign = campaign_repo.get_campaign_any(first)
    assert campaign["name"] == campaign_repo.CONTINUOUS_CAMPAIGN_NAME
    assert campaign["directive_set_id"] and campaign["profile_version_id"]

    # A campaign with any other name does not count as the reserved one.
    campaign_repo.create_campaign(
        ready_seeker,
        {
            "name": "Some other campaign",
            "directive_set_id": campaign["directive_set_id"],
            "profile_version_id": campaign["profile_version_id"],
        },
    )
    assert campaign_repo.get_or_create_continuous_campaign(ready_seeker) == first

    # And the engine stores the id under the seeker's setting key.
    stored = continuous._campaign_for(ready_seeker)
    assert stored == first
    assert (
        admin_repo.get_setting(f"{continuous.SETTING_CAMPAIGN_PREFIX}{ready_seeker}")
        == first
    )


# ---------------------------------------------------------------------------
# contacts
# ---------------------------------------------------------------------------


def _pool(*, normal: bool, retry: bool):
    """A stand-in for ``all_companies_for_contact`` with fixed pool sizes."""

    def fake(limit, **kwargs):
        if kwargs.get("ignore_backoff"):
            return [{"company_id": "retry-co"}] if retry else []
        return [{"company_id": "normal-co"}] if normal else []

    return fake


def test_contacts_phase_starts_bounded_backfill_then_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[str] = []

    async def fake_start(job_id, worker=None):
        started.append(job_id)

    monkeypatch.setattr(runner, "start", fake_start)
    # Both branches have work: a named person without an address, and a
    # company the backoff hides (so only the retry selector sees it).
    monkeypatch.setattr(continuous, "_backfill_pending", lambda: 1)
    monkeypatch.setattr(
        continuous.apply_repo,
        "all_companies_for_contact",
        _pool(normal=False, retry=True),
    )

    first = asyncio.run(continuous.run_phase(force=True, phase="contacts"))
    assert first["mode"] == "backfill"
    assert first["job_kind"] == "contact_email_backfill"
    assert first["limit"] == continuous.CONTACT_LIMIT

    row = query_one("SELECT * FROM job_run WHERE id = ?", (first["job_id"],))
    options = (from_json(row["checkpoint"], {}) or {}).get("options") or {}
    assert options["scope"] == "all"
    assert options["limit"] == continuous.CONTACT_LIMIT
    assert options["backup_methods"] is True
    assert options["crawl_site"] is True
    assert options["allow_smtp"] is False
    assert started == [first["job_id"]]

    second = asyncio.run(continuous.run_phase(force=True, phase="contacts"))
    assert second["mode"] == "discovery"
    assert second["job_kind"] == "contacts_discovery"
    assert second["retry_recent"] is True, "the backoff hides the only available company"
    row = query_one("SELECT * FROM job_run WHERE id = ?", (second["job_id"],))
    options = (from_json(row["checkpoint"], {}) or {}).get("options") or {}
    assert options["scope"] == "all"
    assert options["limit"] == continuous.CONTACT_LIMIT
    assert options["backup_methods"] is True
    assert options["allow_smtp"] is False
    assert options["retry_recent"] is True


def test_contacts_phase_retries_when_the_backoff_hides_the_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A discovery tick ignores the freshness backoff when nothing else has work.

    The live loop reported ``requested: 0`` because all 1,989 no-contact
    companies had been attempted inside the seven-day window, so a
    backoff-respecting discovery selected nothing.  The phase must not spend a
    cycle on an empty pool - it retries the recent verdicts instead, keeping
    the no-contact filter and never re-walking covered companies.
    """
    async def fake_start(job_id, worker=None):
        return None

    monkeypatch.setattr(runner, "start", fake_start)
    monkeypatch.setattr(continuous, "_backfill_pending", lambda: 0)
    monkeypatch.setattr(
        continuous.apply_repo,
        "all_companies_for_contact",
        _pool(normal=False, retry=True),
    )

    report = asyncio.run(continuous.run_phase(force=True, phase="contacts"))

    assert report["mode"] == "discovery"
    assert report["retry_recent"] is True
    row = query_one("SELECT checkpoint FROM job_run WHERE id = ?", (report["job_id"],))
    options = (from_json(row["checkpoint"], {}) or {}).get("options") or {}
    assert options["retry_recent"] is True
    assert options["scope"] == "all"
    assert options["limit"] == continuous.CONTACT_LIMIT
    assert options["backup_methods"] is True
    assert options["crawl_site"] is True


def test_contacts_phase_skips_when_there_is_no_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing to do is reported, not a job that would immediately no-op."""

    async def fake_start(job_id, worker=None):  # pragma: no cover - must not run
        raise AssertionError("no job should be started")

    monkeypatch.setattr(runner, "start", fake_start)
    monkeypatch.setattr(continuous, "_backfill_pending", lambda: 0)
    monkeypatch.setattr(
        continuous.apply_repo,
        "all_companies_for_contact",
        _pool(normal=False, retry=False),
    )

    report = asyncio.run(continuous.run_phase(force=True, phase="contacts"))

    assert report["skipped"] == "no companies without a contact"
    assert report["phase"] == "contacts"


def test_contacts_phase_gives_way_to_a_running_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        continuous, "_contact_job_running", lambda: "contact_email_backfill"
    )

    async def never(*args, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("a job was started on top of a running one")

    monkeypatch.setattr(runner, "start", never)
    report = asyncio.run(continuous.run_phase(force=True, phase="contacts"))
    assert report["skipped"] == "job running"


# ---------------------------------------------------------------------------
# enrich and score
# ---------------------------------------------------------------------------


def test_enrich_runs_the_campaign_passes_with_bounded_limits(
    monkeypatch: pytest.MonkeyPatch, ready_seeker: str
) -> None:
    campaign_id = campaign_repo.get_or_create_continuous_campaign(ready_seeker)
    assert campaign_id
    calls: list[tuple] = []

    async def fake_profile(campaign, seeker, **options):
        calls.append(("profile", campaign, seeker, options.get("limit")))
        return {"profiled": 2}

    async def fake_financial(campaign, seeker, **options):
        calls.append(
            ("financial", campaign, seeker, options.get("limit"), options.get("collect"))
        )
        return {"analysed": 2}

    monkeypatch.setattr(continuous.company_profile, "rerun", fake_profile)
    monkeypatch.setattr(continuous.financial, "rerun", fake_financial)
    admin_repo.set_setting(continuous.SETTING_CURSOR, ready_seeker)
    admin_repo.set_setting(f"{continuous.SETTING_CAMPAIGN_PREFIX}{ready_seeker}", campaign_id)

    report = asyncio.run(continuous.run_phase(force=True, phase="enrich"))

    assert report["campaign_id"] == campaign_id
    assert calls == [
        ("profile", campaign_id, ready_seeker, continuous.ENRICH_LIMIT),
        ("financial", campaign_id, ready_seeker, continuous.ENRICH_LIMIT, True),
    ]


def test_enrich_falls_back_to_the_shared_knowledge_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[str]] = []

    async def fake_enrich(company_ids, **kwargs):
        seen.append(list(company_ids))
        return {"companies": len(company_ids)}

    monkeypatch.setattr(
        continuous.company_enrichment, "enrich_companies", fake_enrich
    )
    monkeypatch.setattr(
        continuous, "_companies_for_shared_enrichment", lambda limit: ["c1", "c2"]
    )

    report = asyncio.run(continuous.run_phase(force=True, phase="enrich"))
    assert seen == [["c1", "c2"]]
    assert report["companies"] == 2


def test_score_scores_the_cursor_seekers_campaign(
    monkeypatch: pytest.MonkeyPatch, ready_seeker: str
) -> None:
    campaign_id = campaign_repo.get_or_create_continuous_campaign(ready_seeker)
    assert campaign_id
    calls: list[tuple] = []

    def fake_score(seeker_id, campaign, **options):
        calls.append((seeker_id, campaign))
        return 3

    monkeypatch.setattr(continuous.scoring, "score_unscored", fake_score)
    admin_repo.set_setting(continuous.SETTING_CURSOR, ready_seeker)
    admin_repo.set_setting(f"{continuous.SETTING_CAMPAIGN_PREFIX}{ready_seeker}", campaign_id)

    report = asyncio.run(continuous.run_phase(force=True, phase="score"))

    assert calls == [(ready_seeker, campaign_id)]
    assert report["scored"] == 3


def test_score_without_a_campaign_is_a_no_op() -> None:
    report = asyncio.run(continuous.run_phase(force=True, phase="score"))
    assert report["skipped"] == "no campaign"


# ---------------------------------------------------------------------------
# Scheduler wiring
# ---------------------------------------------------------------------------


def test_the_scheduler_carries_the_continuous_task() -> None:
    from dreamjob.monitoring import scheduler as scheduler_mod

    task = scheduler_mod.scheduler.task("continuous_collection")
    assert task is not None, "the continuous cycle is not in the scheduler's task table"
    assert task.interval_seconds == 300
    assert task.enabled is True
    assert "enabled" in task.description.lower()


def test_the_scheduler_handler_is_a_no_op_while_disabled() -> None:
    from dreamjob.monitoring import scheduler as scheduler_mod

    continuous.set_enabled(False)
    assert asyncio.run(scheduler_mod._run_continuous()) == {"enabled": False}


def test_the_scheduler_handler_returns_the_phase_report(
    monkeypatch: pytest.MonkeyPatch, phases: list[str]
) -> None:
    from dreamjob.monitoring import scheduler as scheduler_mod

    continuous.set_enabled(True)
    report = asyncio.run(scheduler_mod._run_continuous())
    assert report["phase"] == "discover"
    assert phases == ["discover"]
