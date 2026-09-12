"""Incremental progress for the contacts discovery job (FR-301, NFR-502).

The Contacts screen starts a ``contacts_discovery`` run and polls
``GET /api/contacts/discover/{job_id}`` for the bar and the running report.
This drives the real endpoint, the real runner and the real pipeline through
an authenticated TestClient, with only the two boundaries that would reach the
outside world - the company work list and ``resolve_company`` - replaced by
fast fakes.  If the job never publishes ``progress_total > 1`` or a running
``report``, this test fails and names what was seen.
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

import pytest
from dreamjob.config import get_settings
from dreamjob.db.connection import insert_row, utcnow
from dreamjob.pipeline import apply_contacts as pipeline

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


def _seeker() -> str:
    now = utcnow()
    return insert_row(
        "job_seeker",
        {
            "email": "seeker@example.org",
            "display_name": "Test",
            "created_at": now,
            "updated_at": now,
        },
    )


def _fake_companies(count: int) -> list[dict[str, object]]:
    return [
        {
            "company_id": f"fake-company-{index}",
            "company_name": f"Fake Company {index}",
            "vacancy_count": 1,
        }
        for index in range(count)
    ]


def test_contacts_discovery_reports_incremental_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dreamjob.api.deps import CurrentSeeker, current_admin, current_seeker
    from dreamjob.api.routers import contacts as router_module
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    seeker_id = _seeker()
    companies = _fake_companies(4)

    # The work list is a synchronous repository call made through
    # ``asyncio.to_thread``; the fakes are plain callables returning the four
    # companies.  Both selection functions are replaced so the test holds for
    # either scope, and no network is touched.
    def _work_list(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
        return list(companies)

    monkeypatch.setattr(pipeline.repo, "companies_needing_contact", _work_list)
    monkeypatch.setattr(pipeline.repo, "all_companies_for_contact", _work_list)

    # Hold every company until the test has seen the start tick, so the slow
    # observation side of the assertion is not a race against a 4-item pass.
    # Once released, finish the companies a little apart so each progress tick
    # is visible to a poller rather than collapsing into one burst.
    release = threading.Event()

    async def _resolve_company(company: dict, **_kwargs: object) -> pipeline.CompanyOutcome:
        while not release.is_set():
            await asyncio.sleep(0.01)
        index = int(str(company["company_id"]).rsplit("-", 1)[-1])
        await asyncio.sleep(0.15 * (index + 1))
        return pipeline.CompanyOutcome(
            company_id=str(company["company_id"]),
            company_name=str(company["company_name"]),
            vacancy_count=int(company.get("vacancy_count") or 0),
            status="reachable",
            domain="fake.example",
            domain_source="test",
            email="hello@fake.example",
        )

    monkeypatch.setattr(pipeline, "resolve_company", _resolve_company)

    me = CurrentSeeker(
        id=seeker_id,
        email="seeker@example.org",
        display_name="Test",
        is_admin=False,
        locale="en",
    )
    app = FastAPI()
    app.include_router(router_module.router, prefix="/api/contacts")
    app.dependency_overrides[current_seeker] = lambda: me
    app.dependency_overrides[current_admin] = lambda: me

    with TestClient(app) as client:
        started = client.post("/api/contacts/discover", json={"limit": 4, "scope": "all"})
        assert started.status_code == 202, started.text
        job_id = started.json()["job_id"]

        def status() -> dict:
            response = client.get(f"/api/contacts/discover/{job_id}")
            assert response.status_code == 200, response.text
            return response.json()

        # Wait for the pipeline to publish the work-list size (the start tick)
        # before letting any company resolve.
        row: dict = {}
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            row = status()
            if row.get("progress_total") == 4:
                break
            assert row["status"] not in ("failed", "cancelled"), row
            time.sleep(0.02)

        assert row.get("progress_total") == 4, f"never saw total=4; last row={row}"

        start_report = row.get("report") or {}
        assert start_report.get("scope") == "all", start_report

        release.set()

        observed_done: set[int] = set()
        observed_totals: set[int] = set()
        saw_running_report = False
        terminal: dict = {}
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            row = status()
            observed_done.add(int(row.get("progress_done") or 0))
            observed_totals.add(int(row.get("progress_total") or 0))
            report = row.get("report") or {}
            if int(report.get("companies_visited") or 0) > 0:
                saw_running_report = True
            if row["status"] in ("done", "failed", "cancelled"):
                terminal = row
                break
            time.sleep(0.01)

        assert terminal, f"job never reached a terminal state; last row={row}"
        assert terminal["status"] == "done", terminal
        assert 4 in observed_totals, f"progress_total never reached 4; saw {sorted(observed_totals)}"
        assert max(observed_done) >= 3, f"progress_done did not advance; saw {sorted(observed_done)}"
        assert saw_running_report, "no report with running counters was ever visible"

        final_report = terminal.get("report") or {}
        assert final_report.get("companies_visited") == 4, final_report
