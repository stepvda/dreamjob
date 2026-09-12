"""The FR-303 backup contact sources (FR-303, FR-304, FR-305, NFR-302).

The backup stage runs only when the normal ladder finds nothing, and it reads
three sources in order: the postings the corpus already holds, the employer's
ATS board, and - when the board names an employer domain - the ordinary site
crawl and the conventional mailboxes.  Everything here is offline: the egress
client is a stub and DNS is stubbed at the validation boundary, so the
assertions are about *which* source produced an address, what label it carries
and whether it is allowed to be stored as certain.
"""

from __future__ import annotations

import asyncio
import base64
import os
import secrets
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from dreamjob.config import get_settings
from dreamjob.db.connection import from_json, insert_row, query_one, utcnow
from dreamjob.pipeline import apply_contacts as ladder
from dreamjob.pipeline import contact_backup
from dreamjob.pipeline import email_patterns as patterns
from dreamjob.pipeline import email_validate as validation

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
    os.environ["DREAMJOB_MASTER_KEY"] = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
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
# Fixtures
# ---------------------------------------------------------------------------


def _company(name: str, *, domain: str | None = None, country: str = "BE", **extra: Any) -> str:
    return insert_row(
        "company",
        {
            "name": name,
            "normalised_name": name.lower(),
            "domain": domain,
            "country": country,
            "collected_at": utcnow(),
            **extra,
        },
    )


def _vacancy(company_id: str, title: str = "Data Engineer", **extra: Any) -> str:
    return insert_row(
        "vacancy",
        {
            "company_id": company_id,
            "title": title,
            "country": "BE",
            "posted_at": utcnow(),
            "collected_at": utcnow(),
            **extra,
        },
    )


def _seekers() -> dict[str, str]:
    now = utcnow()
    seeker_id = insert_row(
        "job_seeker",
        {"email": "seeker@example.org", "display_name": "Test", "created_at": now,
         "updated_at": now},
    )
    profile_id = insert_row(
        "profile_version",
        {"job_seeker_id": seeker_id, "version": 1, "sections": {}, "created_at": now},
    )
    directive_id = insert_row(
        "directive_set", {"job_seeker_id": seeker_id, "name": "default", "created_at": now}
    )
    campaign_id = insert_row(
        "campaign",
        {"job_seeker_id": seeker_id, "directive_set_id": directive_id,
         "profile_version_id": profile_id, "name": "spring", "status": "running",
         "created_at": now},
    )
    return {"seeker_id": seeker_id, "campaign_id": campaign_id}


def _work_row(company_id: str, name: str) -> dict[str, Any]:
    return {
        "company_id": company_id,
        "company_name": name,
        "company_domain": None,
        "careers_url": None,
        "company_country": "BE",
        "vacancy_count": 1,
    }


def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        validation,
        "_resolve_mx",
        lambda domain, timeout=5.0: validation.MXResult(has_mx=True, hosts=["mx.test"]),
    )
    monkeypatch.setattr(validation.limits.__class__, "smtp_enabled", property(lambda self: False))


class _Page:
    """The shape of the egress client's answer, without the egress client."""

    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


class _Egress:
    """A stub egress client: one canned page per URL, and a request counter."""

    def __init__(self, pages: dict[str, str], status: int = 200) -> None:
        self.pages = pages
        self.status = status
        self.requested: list[str] = []

    async def fetch(self, url: str, **_: Any) -> _Page:
        self.requested.append(url)
        for key, body in self.pages.items():
            if url.rstrip("/").endswith(key.rstrip("/")) or key in url:
                return _Page(body, self.status)
        return _Page("", 404)


# ---------------------------------------------------------------------------
# Stored postings: the corpus already holds the address, so nothing is fetched
# ---------------------------------------------------------------------------


def test_a_stored_posting_yields_an_address_without_a_fetch() -> None:
    company_id = _company("Acme Data BV")
    _vacancy(
        company_id,
        application_target="jobs@acme-data.example",
        description="Stuur je cv naar sollicitatie@acme-data.example voor 1 oktober.",
    )
    egress = _Egress({})
    findings, domain, note = asyncio.run(
        contact_backup.harvest_backup_addresses(
            {"company_id": company_id, "company_name": "Acme Data BV"}, egress=egress
        )
    )
    emails = [finding.address.email for finding in findings]
    assert "jobs@acme-data.example" in emails
    assert "sollicitatie@acme-data.example" in emails
    assert all(
        finding.address.method == patterns.METHOD_STORED_DOCUMENT for finding in findings
    )
    assert findings[0].address.confidence == pytest.approx(0.45)
    assert domain is None
    assert "stored_document" in note
    # The stored source is free: no page was fetched to answer it.
    assert egress.requested == []


def test_an_aggregator_address_in_a_posting_is_not_the_employers() -> None:
    """The board's own boilerplate must not become the employer's contact."""
    company_id = _company("Acme Data BV")
    _vacancy(company_id, description="Questions? Write to helpdesk@europa.eu.")
    findings, _domain, note = asyncio.run(
        contact_backup.harvest_backup_addresses(
            {"company_id": company_id, "company_name": "Acme Data BV"}, egress=_Egress({})
        )
    )
    assert findings == []
    assert "no address found" in note


def test_noise_addresses_are_skipped() -> None:
    """``noreply@`` and an inlined asset are not somebody to write to."""
    found = patterns.addresses_in_text(
        "noreply@acme.be logo@2x.png jobs@acme.be", method=patterns.METHOD_STORED_DOCUMENT
    )
    assert [item.email for item in found] == ["jobs@acme.be"]


# ---------------------------------------------------------------------------
# The ATS board, and the domain it names
# ---------------------------------------------------------------------------


def test_an_ats_board_yields_an_address_and_the_employer_domain() -> None:
    company_id = _company("Acme Data BV", ats_vendor="greenhouse", ats_slug="acme")
    _vacancy(company_id)
    board = (
        "<html><head><script type=\"application/ld+json\">"
        '{"@type":"JobPosting","title":"Data Engineer",'
        '"hiringOrganization":{"@type":"Organization","name":"Acme Data",'
        '"url":"https://acmedata.be"}}'
        "</script><title>Acme Data careers</title></head>"
        "<body><p>Questions? sollicitatie@acmedata.be</p>"
        "<p>Platform support: help@greenhouse.io</p>"
        "<a href='https://partner.example/overview'>Partner</a></body></html>"
    )
    home = (
        "<title>Acme Data</title><body><p>Acme Data builds pipelines. "
        "Write to jobs@acmedata.be.</p></body>"
    )
    egress = _Egress({"job-boards.greenhouse.io/acme": board, "acmedata.be": home})
    findings, domain, note = asyncio.run(
        contact_backup.harvest_backup_addresses(
            {"company_id": company_id, "company_name": "Acme Data BV"},
            egress=egress,
            crawl_site=True,
        )
    )
    by_email = {finding.address.email: finding for finding in findings}
    # The board's tenant address is published there and labelled as such.
    assert by_email["sollicitatie@acmedata.be"].address.method == patterns.METHOD_ATS_BOARD
    assert by_email["sollicitatie@acmedata.be"].address.confidence == pytest.approx(0.6)
    # The board's own support address is not the employer's.
    assert "help@greenhouse.io" not in by_email
    # The JSON-LD hiringOrganization URL recovered the employer domain, and the
    # ordinary crawl/generics ran on it with their own labels.
    assert domain == "acmedata.be"
    assert by_email["jobs@acmedata.be"].address.method == patterns.METHOD_WEBSITE
    assert by_email["careers@acmedata.be"].address.method == patterns.METHOD_PATTERN
    assert "ats_board" in note
    assert any("greenhouse.io" in url for url in egress.requested)


def test_the_most_common_vacancy_host_is_the_last_board_fallback() -> None:
    company_id = _company("Acme Data BV")
    for _ in range(3):
        _vacancy(company_id, source_url="https://acme.jobs.example/posting/1")
    egress = _Egress({"acme.jobs.example": "<title>Acme</title><p>jobs@acmedata.be</p>"})
    findings, _domain, _note = asyncio.run(
        contact_backup.harvest_backup_addresses(
            {"company_id": company_id, "company_name": "Acme Data BV"},
            egress=egress,
            crawl_site=False,
        )
    )
    assert [finding.address.email for finding in findings] == ["jobs@acmedata.be"]
    assert findings[0].address.method == patterns.METHOD_ATS_BOARD
    assert egress.requested == ["https://acme.jobs.example"]


# ---------------------------------------------------------------------------
# The ladder: backup only when the normal sources found nothing
# ---------------------------------------------------------------------------


def test_backup_runs_only_when_the_ladder_finds_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A company the ordinary path can answer must cost no backup fetch."""
    _offline(monkeypatch)
    company_id = _company("Acme Data BV", domain="acme-data.example")
    _vacancy(
        company_id,
        application_channel="email",
        application_target="jobs@acme-data.example",
    )
    egress = _Egress({})
    calls: list[dict[str, Any]] = []

    async def _record(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        return [], None, "backup ran"

    monkeypatch.setattr(ladder.contact_backup, "harvest_backup_addresses", _record)
    outcome = asyncio.run(
        ladder.resolve_company(
            _work_row(company_id, "Acme Data BV"), egress=egress, crawl_site=False, backup=True
        )
    )
    assert outcome.reachable
    assert outcome.method == patterns.METHOD_VACANCY
    assert calls == []
    assert egress.requested == []


def test_the_backup_stage_answers_when_the_ladder_cannot(monkeypatch: pytest.MonkeyPatch) -> None:
    """No domain and no normal source: the ATS board still reaches them."""
    _offline(monkeypatch)
    company_id = _company("Acme Data BV", ats_vendor="recruitee", ats_slug="acme")
    _vacancy(company_id)
    board = "<title>Acme careers</title><p>Mail sollicitatie@acmedata.be.</p>"
    egress = _Egress({"acme.recruitee.com": board})
    outcome = asyncio.run(
        ladder.resolve_company(
            _work_row(company_id, "Acme Data BV"),
            egress=egress,
            crawl_site=False,
            derive_domains=False,
            backup=True,
        )
    )
    assert outcome.reachable
    assert outcome.email == "sollicitatie@acmedata.be"
    assert outcome.method == patterns.METHOD_ATS_BOARD
    row = query_one("SELECT * FROM contact WHERE company_id = ?", (company_id,))
    assert row["email_source_method"] == patterns.METHOD_ATS_BOARD
    # A published address is not a guess.
    assert row["email_uncertain"] == 0


def test_an_unreachable_backup_attempt_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """``unreachable_reasons`` must show that the backup stage was tried."""
    _offline(monkeypatch)
    company_id = _company("Nowhere BV")
    _vacancy(company_id)
    egress = _Egress({})
    outcome = asyncio.run(
        ladder.resolve_company(
            _work_row(company_id, "Nowhere BV"),
            egress=egress,
            crawl_site=False,
            derive_domains=False,
            backup=True,
        )
    )
    assert outcome.status == "unreachable"
    reason = query_one(
        "SELECT reason FROM apply_contact_resolution WHERE company_id = ?", (company_id,)
    )["reason"]
    assert reason.lower().startswith("backup attempted")
    assert ladder._reason_bucket(reason).startswith("backup")


def test_a_composed_generic_from_a_recovered_domain_is_stored_uncertain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A composed mailbox stays a hypothesis; migration 151 says so."""
    _offline(monkeypatch)
    company_id = _company("Acme Data BV", ats_vendor="greenhouse", ats_slug="acme")
    _vacancy(company_id)
    board = (
        '<script type="application/ld+json">'
        '{"@type":"JobPosting","hiringOrganization":{"url":"https://acmedata.be"}}'
        "</script><title>Acme Data careers</title>"
    )
    home = "<title>Acme Data</title><body><p>Acme Data builds pipelines.</p></body>"
    egress = _Egress({"job-boards.greenhouse.io/acme": board, "acmedata.be": home})
    outcome = asyncio.run(
        ladder.resolve_company(
            _work_row(company_id, "Acme Data BV"),
            egress=egress,
            crawl_site=True,
            derive_domains=False,
            backup=True,
        )
    )
    assert outcome.reachable
    assert outcome.domain == "acmedata.be"
    assert outcome.domain_source == ladder.SOURCE_BACKUP
    assert outcome.method == patterns.METHOD_PATTERN
    assert outcome.is_generic
    row = query_one("SELECT * FROM contact WHERE company_id = ?", (company_id,))
    assert row["email_source_method"] == patterns.METHOD_PATTERN
    assert row["email_uncertain"] == 1


# ---------------------------------------------------------------------------
# The API flag: ``backup_methods`` reaches the checkpoint and the endpoints
# ---------------------------------------------------------------------------


def _app_and_seeker(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, dict[str, str]]:
    from dreamjob.api.deps import CurrentSeeker, current_admin, current_seeker
    from dreamjob.api.routers import contacts as router_module
    from fastapi import FastAPI

    seed = _seekers()
    me = CurrentSeeker(
        id=seed["seeker_id"], email="seeker@example.org", display_name="Test",
        is_admin=True, locale="en",
    )
    app = FastAPI()
    app.include_router(router_module.router, prefix="/api/contacts")
    app.dependency_overrides[current_seeker] = lambda: me
    app.dependency_overrides[current_admin] = lambda: me

    async def _no_start(job_id: str, *args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(router_module.runner, "start", _no_start)
    return app, seed


def test_the_per_company_endpoint_forwards_backup_methods(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dreamjob.api.routers import contacts as router_module
    from fastapi.testclient import TestClient

    app, _seed = _app_and_seeker(monkeypatch)
    company_id = _company("Acme Data BV")
    captured: dict[str, Any] = {}

    async def _fake(company_id_: str, **kwargs: Any) -> Any:
        captured.update(kwargs, company_id=company_id_)
        return router_module.scale_pipeline.CompanyOutcome(
            company_id=company_id_,
            company_name="Acme Data BV",
            vacancy_count=0,
            status="unreachable",
            reason="fake",
        )

    monkeypatch.setattr(router_module.scale_pipeline, "resolve_company_by_id", _fake)
    with TestClient(app) as client:
        response = client.post(
            f"/api/contacts/companies/{company_id}/discover",
            json={"backup_methods": True, "crawl_site": False},
        )
    assert response.status_code == 200, response.text
    assert captured["backup"] is True
    assert captured["company_id"] == company_id


def test_backup_methods_travels_in_the_discovery_job_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flag is stored with the options the worker resumes from (NFR-401)."""
    from fastapi.testclient import TestClient

    app, _seed = _app_and_seeker(monkeypatch)
    with TestClient(app) as client:
        started = client.post(
            "/api/contacts/discover",
            json={
                "limit": 5,
                "backup_methods": True,
                "crawl_site": False,
                "derive_domains": False,
            },
        )
    assert started.status_code == 202, started.text
    job_id = started.json()["job_id"]
    row = query_one("SELECT checkpoint FROM job_run WHERE id = ?", (job_id,))
    options = from_json(row["checkpoint"], {})["options"]
    assert options["backup_methods"] is True
    assert options["crawl_site"] is False


def test_backup_methods_travels_in_the_backfill_job_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    app, _seed = _app_and_seeker(monkeypatch)
    with TestClient(app) as client:
        started = client.post(
            "/api/contacts/emails/backfill",
            json={"limit": 5, "backup_methods": True, "crawl_site": False},
        )
    assert started.status_code == 202, started.text
    row = query_one("SELECT checkpoint FROM job_run WHERE id = ?", (started.json()["job_id"],))
    options = from_json(row["checkpoint"], {})["options"]
    assert options["backup_methods"] is True


def test_the_discovery_worker_translates_backup_methods(monkeypatch: pytest.MonkeyPatch) -> None:
    """The checkpoint keeps the request name; the pass takes ``backup``."""
    captured: dict[str, Any] = {}

    async def _fake(job_seeker_id: str, limit: int, **kwargs: Any) -> ladder.ApplyContactsReport:
        captured.update(kwargs, job_seeker_id=job_seeker_id, limit=limit)
        return ladder.ApplyContactsReport(job_seeker_id=job_seeker_id, requested=limit)

    monkeypatch.setattr(ladder, "ensure_apply_contacts", _fake)

    class _Ctx:
        job_seeker_id = "seeker-1"
        campaign_id = None
        checkpoint = {"options": {"backup_methods": True, "limit": 3}}

        def save_checkpoint(self, **kwargs: Any) -> None:
            pass

        def progress(self, done: int, total: int | None = None) -> None:
            pass

    async def run() -> None:
        async for _ in ladder.contacts_discovery_worker(_Ctx()):  # type: ignore[arg-type]
            pass

    asyncio.run(run())
    assert captured["backup"] is True
    assert "backup_methods" not in captured


def test_backup_findings_keep_their_labels_in_the_ranking() -> None:
    """A published finding is sized like published evidence, not like a guess."""
    published = ladder.backup_candidates(
        [
            contact_backup.BackupFinding(
                patterns.FoundAddress(
                    email="sollicitatie@acmedata.be",
                    method=patterns.METHOD_ATS_BOARD,
                    confidence=patterns.METHOD_CONFIDENCE[patterns.METHOD_ATS_BOARD],
                ),
                "board",
            ),
            contact_backup.BackupFinding(
                patterns.FoundAddress(
                    email="careers@acmedata.be",
                    method=patterns.METHOD_PATTERN,
                    confidence=0.4,
                ),
                "generic",
            ),
        ]
    )
    assert [c.email_source_method for c in published] == [
        patterns.METHOD_ATS_BOARD,
        patterns.METHOD_PATTERN,
    ]
    ats = next(c for c in published if c.email_source_method == patterns.METHOD_ATS_BOARD)
    inferred = next(c for c in published if c.email_source_method == patterns.METHOD_PATTERN)
    assert ladder.discovery.score_candidate(ats) > ladder.discovery.score_candidate(inferred)
    assert not ats.email_uncertain
