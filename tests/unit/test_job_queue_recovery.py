"""A queued job must survive a restart, and a repeated click must not queue twice.

Two defects meet in this file.  The job pool is in memory (``jobs/runner.py``),
so a job ``start()``ed while every slot is busy waits in a queue that a restart
throws away; it used to be left ``pending`` with no marker, which
``dispatch_recoverable`` never selected, so it was stranded for good.  And a
heavy sweep started twice fills all the pool slots with the same work, so a
small job behind it never runs.

The tests pin the fixes without touching the network or a real model: the
runner's own bookkeeping is exercised against a throw-away migrated database,
the endpoints are driven through an authenticated ``TestClient`` with a single
variable in play at a time, and the backfill's only outside step - the MX lookup
- is replaced with one the test can hold and release.
"""

from __future__ import annotations

import asyncio
import base64
import os
import secrets
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from dreamjob.config import get_settings
from dreamjob.db.connection import insert_row, query_one, update_row, utcnow
from dreamjob.jobs.runner import QUEUED_ERROR_MARKER, JobContext, JobRunner

_ENV_KEYS = (
    "DREAMJOB_DATA_DIR",
    "DREAMJOB_DB_PATH",
    "DREAMJOB_MASTER_KEY",
    "DREAMJOB_SESSION_SECRET",
    "DREAMJOB_ENV",
    "DEEPSEEK_API_KEY",
)


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path) -> Iterator[None]:
    """A throwaway database, migrated from scratch for every test."""
    saved = {k: os.environ.get(k) for k in _ENV_KEYS}
    os.environ["DREAMJOB_DATA_DIR"] = str(tmp_path / "data")
    os.environ["DREAMJOB_DB_PATH"] = str(tmp_path / "data" / "test.db")
    os.environ["DREAMJOB_MASTER_KEY"] = base64.urlsafe_b64encode(
        secrets.token_bytes(32)
    ).decode()
    os.environ["DREAMJOB_SESSION_SECRET"] = secrets.token_urlsafe(32)
    os.environ["DREAMJOB_ENV"] = "development"
    os.environ["DEEPSEEK_API_KEY"] = ""
    get_settings.cache_clear()

    from dreamjob.db.migrator import migrate

    migrate()
    yield

    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Fixtures and instruments
# ---------------------------------------------------------------------------


class _RecordingPool:
    """Stands in for ``_DaemonPool``: records the submit, runs nothing.

    Used to observe the row in the instant between ``start()`` persisting the
    queued marker and the job actually beginning, without racing a real pool
    thread to the read.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[Any, tuple[Any, ...]]] = []

    def submit(self, fn: Any, *args: Any) -> None:
        self.calls.append((fn, args))

    def queued(self) -> int:
        return 0


def _row(job_id: str) -> dict:
    row = query_one("SELECT * FROM job_run WHERE id = ?", (job_id,))
    assert row is not None
    return row


def _seeker(email: str = "seeker@example.org") -> str:
    now = utcnow()
    return insert_row(
        "job_seeker",
        {"email": email, "display_name": "Test", "created_at": now, "updated_at": now},
    )


def _company(name: str, domain: str) -> str:
    return insert_row(
        "company",
        {
            "name": name,
            "normalised_name": name.lower(),
            "domain": domain,
            "country": "BE",
            "collected_at": utcnow(),
        },
    )


def _contact(company_id: str, name: str) -> str:
    return insert_row(
        "contact",
        {
            "company_id": company_id,
            "full_name": name,
            "source": "test",
            "collected_at": utcnow(),
            "shareable": 1,
        },
    )


def _current(seeker_id: str, *, is_admin: bool = False) -> Any:
    from dreamjob.api.deps import CurrentSeeker

    return CurrentSeeker(
        id=seeker_id,
        email="seeker@example.org",
        display_name="Test",
        is_admin=is_admin,
        locale="en",
    )


def _app(router_module: Any, me: Any) -> Any:
    from dreamjob.api.deps import current_admin, current_seeker
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(router_module.router, prefix="/api/contacts")
    app.dependency_overrides[current_seeker] = lambda: me
    app.dependency_overrides[current_admin] = lambda: me
    return app


# ---------------------------------------------------------------------------
# 1. The marker is written when queued, and cleared when the job runs
# ---------------------------------------------------------------------------


async def test_start_marks_a_pending_job_queued_and_run_clears_it() -> None:
    runner = JobRunner()
    pool = _RecordingPool()
    runner._pool = pool  # type: ignore[assignment]  # the pool is the seam under test
    job_id = runner.create("test.queue", total=1)
    assert _row(job_id)["status"] == "pending"
    assert _row(job_id)["last_error"] is None

    release = threading.Event()

    async def worker(ctx: JobContext) -> None:
        await asyncio.to_thread(release.wait, 10)

    await runner.start(job_id, worker)

    # ``start()`` persisted the intent; the pool captured the job rather than
    # running it, so the row is still pending with the resumable marker.
    assert pool.calls, "start() did not hand the job to the pool"
    row = _row(job_id)
    assert row["status"] == "pending"
    assert row["last_error"] == QUEUED_ERROR_MARKER

    # Now run the body the way the pool thread would, holding it inside the
    # worker so the running write is observable rather than instantaneous.
    _isolate, (ctx, fn) = pool.calls[-1]
    task = asyncio.create_task(runner._run(ctx, fn))
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        row = _row(job_id)
        if row["status"] == "running":
            break
        await asyncio.sleep(0.01)
    assert row["status"] == "running", row
    assert row["last_error"] is None, "running must clear the resumable marker"

    release.set()
    await asyncio.wait_for(task, timeout=10)
    assert _row(job_id)["status"] == "done"
    # Let the loop watchdog notice the job is gone and retire.
    await asyncio.sleep(0.3)


# ---------------------------------------------------------------------------
# 2. A restart re-dispatches a lost-but-queued job
# ---------------------------------------------------------------------------


async def test_dispatch_recoverable_restarts_a_queued_job_after_a_restart() -> None:
    # A fresh runner is what a process restart gives you: the in-memory queue
    # and its controls are gone, and only the row is left to recover from.
    restarted = JobRunner()
    job_id = restarted.create("test.queue_recovery", total=1)
    update_row("job_run", job_id, {"last_error": QUEUED_ERROR_MARKER})

    ran = threading.Event()

    async def worker(ctx: JobContext) -> None:
        ctx.save_checkpoint(ran=True)
        ran.set()

    restarted.register_worker("test.queue_recovery", worker)

    started = await restarted.dispatch_recoverable()
    assert started == 1, "a pending job carrying the marker must be re-dispatched"

    deadline = time.monotonic() + 10
    row = {}
    while time.monotonic() < deadline:
        row = _row(job_id)
        if row["status"] in ("done", "failed", "cancelled"):
            break
        await asyncio.sleep(0.02)

    assert ran.wait(1), "the recovered job never ran"
    assert row["status"] == "done", row
    assert row["last_error"] is None
    assert (query_one("SELECT 1 FROM job_run WHERE id = ? AND status = 'done'", (job_id,)))


async def test_dispatch_recoverable_ignores_a_bare_pending_row() -> None:
    """A created-but-never-started job has no marker and is still not picked up."""
    restarted = JobRunner()
    job_id = restarted.create("test.bare_pending", total=1)

    async def worker(ctx: JobContext) -> None:  # pragma: no cover - must not run
        raise AssertionError("a bare pending row must not be dispatched")

    restarted.register_worker("test.bare_pending", worker)
    assert await restarted.dispatch_recoverable() == 0
    assert _row(job_id)["status"] == "pending"


# ---------------------------------------------------------------------------
# 3. The endpoints reuse the job that is already running or queued
# ---------------------------------------------------------------------------


def _no_network_start(monkeypatch: pytest.MonkeyPatch, router_module: Any) -> None:
    """Replace ``start`` with one that only writes the queued marker.

    The endpoint tests are about the dedupe decision, not about running the
    pass, so the job stays ``pending`` exactly as it would if the pool were
    full - which is the state the dedupe has to recognise.
    """

    async def _queued_start(job_id: str, *args: object, **kwargs: object) -> None:
        update_row("job_run", job_id, {"last_error": QUEUED_ERROR_MARKER})

    monkeypatch.setattr(router_module.runner, "start", _queued_start)


def test_discover_endpoint_reuses_a_running_or_queued_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dreamjob.api.routers import contacts as router_module
    from fastapi.testclient import TestClient

    seeker_id = _seeker()
    _no_network_start(monkeypatch, router_module)
    app = _app(router_module, _current(seeker_id))

    with TestClient(app) as client:
        first = client.post("/api/contacts/discover", json={"scope": "all", "limit": 4})
        assert first.status_code == 202, first.text
        first_body = first.json()
        assert first_body["reused"] is False

        # The queued job (marker written by start) is handed back, not doubled.
        second = client.post("/api/contacts/discover", json={"scope": "all", "limit": 4})
        assert second.status_code == 202, second.text
        assert second.json()["reused"] is True
        assert second.json()["job_id"] == first_body["job_id"]

        # A running job matches too, marker or not.
        update_row("job_run", first_body["job_id"], {"status": "running", "last_error": None})
        third = client.post("/api/contacts/discover", json={"scope": "all", "limit": 4})
        assert third.json()["reused"] is True
        assert third.json()["job_id"] == first_body["job_id"]

        # A different scope is a different sweep and gets its own job.
        other = client.post("/api/contacts/discover", json={"scope": "shortlist", "limit": 4})
        assert other.status_code == 202, other.text
        assert other.json()["reused"] is False
        assert other.json()["job_id"] != first_body["job_id"]


def test_backfill_endpoint_reuses_a_running_or_queued_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dreamjob.api.routers import contacts as router_module
    from fastapi.testclient import TestClient

    seeker_id = _seeker()
    _no_network_start(monkeypatch, router_module)
    app = _app(router_module, _current(seeker_id))

    with TestClient(app) as client:
        first = client.post(
            "/api/contacts/emails/backfill", json={"scope": "mine", "limit": 10}
        )
        assert first.status_code == 202, first.text
        first_body = first.json()
        assert first_body["reused"] is False

        second = client.post(
            "/api/contacts/emails/backfill", json={"scope": "mine", "limit": 10}
        )
        assert second.json()["reused"] is True
        assert second.json()["job_id"] == first_body["job_id"]

        # Once the job is terminal it is no longer reusable; a new one starts.
        update_row(
            "job_run",
            first_body["job_id"],
            {"status": "done", "finished_at": utcnow(), "last_error": None},
        )
        third = client.post(
            "/api/contacts/emails/backfill", json={"scope": "mine", "limit": 10}
        )
        assert third.status_code == 202, third.text
        assert third.json()["reused"] is False
        assert third.json()["job_id"] != first_body["job_id"]


def test_backfill_endpoint_does_not_reuse_a_stranded_bare_pending_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``pending`` row with no marker is what the old code stranded.

    Reusing it would hand the caller a job that nothing will ever start, so a
    fresh one must be created instead.
    """
    from dreamjob.api.routers import contacts as router_module
    from dreamjob.pipeline import contact_email_backfill as backfill
    from fastapi.testclient import TestClient

    seeker_id = _seeker()
    stranded = insert_row(
        "job_run",
        {
            "job_seeker_id": seeker_id,
            "kind": backfill.BACKFILL_JOB_KIND,
            "status": "pending",
            "created_at": utcnow(),
            "checkpoint": '{"options": {"scope": "mine"}}',
        },
    )
    _no_network_start(monkeypatch, router_module)
    app = _app(router_module, _current(seeker_id))

    with TestClient(app) as client:
        started = client.post(
            "/api/contacts/emails/backfill", json={"scope": "mine", "limit": 10}
        )
        assert started.status_code == 202, started.text
        assert started.json()["reused"] is False
        assert started.json()["job_id"] != stranded


# ---------------------------------------------------------------------------
# 4. End to end: the backfill bar reports every company and advances
# ---------------------------------------------------------------------------


def test_backfill_endpoint_reports_company_progress_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /emails/backfill, then poll the status route while it works.

    The only outside step - the MX lookup FR-304 runs after the syntax check -
    is held until the test has seen the start tick, so the total is observable
    and the done counter advancing is not a race against a three-company pass.
    """
    from dreamjob.api.routers import contacts as router_module
    from dreamjob.pipeline import email_validate as validation
    from fastapi.testclient import TestClient

    seeker_id = _seeker()
    for index in range(3):
        company_id = _company(f"Acme {index} BV", f"acme{index}.example")
        _contact(company_id, "Marie Dupont")

    release = threading.Event()

    def _slow_mx(domain: str, timeout: float = 5.0) -> Any:
        release.wait(timeout=15)
        return validation.MXResult(has_mx=True, hosts=[f"mx.{domain}"])

    monkeypatch.setattr(validation, "_resolve_mx", _slow_mx)

    app = _app(router_module, _current(seeker_id))

    with TestClient(app) as client:
        started = client.post(
            "/api/contacts/emails/backfill",
            json={"scope": "mine", "limit": 10, "crawl_site": False},
        )
        assert started.status_code == 202, started.text
        job_id = started.json()["job_id"]

        def status() -> dict:
            response = client.get(f"/api/contacts/emails/backfill/{job_id}")
            assert response.status_code == 200, response.text
            return response.json()

        row: dict = {}
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            row = status()
            if row.get("progress_total") == 3:
                break
            assert row["status"] not in ("failed", "cancelled"), row
            time.sleep(0.02)
        assert row.get("progress_total") == 3, f"never saw total=3; last row={row}"

        start_report = (row.get("checkpoint") or {}).get("report") or {}
        assert start_report.get("considered") == 3, start_report

        release.set()

        observed_done: set[int] = set()
        terminal: dict = {}
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            row = status()
            observed_done.add(int(row.get("progress_done") or 0))
            if row["status"] in ("done", "failed", "cancelled"):
                terminal = row
                break
            time.sleep(0.01)

        assert terminal, f"job never reached a terminal state; last row={row}"
        assert terminal["status"] == "done", terminal
        assert terminal["last_error"] is None, terminal
        assert max(observed_done) >= 1, f"progress_done never advanced; saw {observed_done}"

        report = (terminal.get("checkpoint") or {}).get("report") or {}
        assert report.get("companies_visited") == 3, report
        assert report.get("considered") == 3, report
        assert report.get("updated") == 3, report
