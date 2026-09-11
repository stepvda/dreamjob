"""Filling e-mail addresses into stored contacts that have none
(FR-303, FR-304, FR-344, NFR-302, NFR-303, NFR-502).

Everything here runs against a throwaway SQLite file with no DNS, no HTTP and
no SMTP.  The rules under test are the ones that decide whether an inferred
address may be stored (FR-303/FR-304), who the work list may touch (FR-344,
NFR-302, NFR-303) and whether running the pass twice does anything the second
time.  The network-facing steps are stubbed at the validation boundary, which
is where the verdict - not the fetch - is decided.
"""

from __future__ import annotations

import asyncio
import base64
import os
import secrets
from collections.abc import Iterator
from pathlib import Path

import pytest
from dreamjob.config import get_settings
from dreamjob.db.connection import from_json, insert_row, query_one, utcnow
from dreamjob.db.repositories import contacts as repo
from dreamjob.pipeline import contact_email_backfill as backfill
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
    """A throwaway database, migrated from scratch for every test."""
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
# Fixtures: two seekers, one company with a domain
# ---------------------------------------------------------------------------


def _seed() -> dict[str, str]:
    now = utcnow()
    seeker_id = insert_row(
        "job_seeker",
        {
            "email": "seeker@example.org",
            "display_name": "Stephane van der Aa",
            "created_at": now,
            "updated_at": now,
        },
    )
    other_id = insert_row(
        "job_seeker",
        {
            "email": "other@example.org",
            "display_name": "Other Seeker",
            "created_at": now,
            "updated_at": now,
        },
    )
    profile_id = insert_row(
        "profile_version",
        {"job_seeker_id": seeker_id, "version": 1, "sections": {}, "created_at": now},
    )
    other_profile = insert_row(
        "profile_version",
        {"job_seeker_id": other_id, "version": 1, "sections": {}, "created_at": now},
    )
    directive_id = insert_row(
        "directive_set", {"job_seeker_id": seeker_id, "name": "default", "created_at": now}
    )
    other_directive = insert_row(
        "directive_set", {"job_seeker_id": other_id, "name": "default", "created_at": now}
    )
    campaign_id = insert_row(
        "campaign",
        {
            "job_seeker_id": seeker_id,
            "directive_set_id": directive_id,
            "profile_version_id": profile_id,
            "name": "spring",
            "status": "running",
            "created_at": now,
        },
    )
    other_campaign = insert_row(
        "campaign",
        {
            "job_seeker_id": other_id,
            "directive_set_id": other_directive,
            "profile_version_id": other_profile,
            "name": "other",
            "status": "running",
            "created_at": now,
        },
    )
    company_id = insert_row(
        "company",
        {
            "name": "Acme Data BV",
            "normalised_name": "acme data",
            "domain": "acme-data.example",
            "country": "BE",
            "careers_url": "https://acme-data.example/jobs",
            "collected_at": now,
        },
    )
    return {
        "seeker_id": seeker_id,
        "other_id": other_id,
        "campaign_id": campaign_id,
        "other_campaign": other_campaign,
        "company_id": company_id,
    }


def _contact(
    company_id: str,
    *,
    email: str | None = None,
    name: str = "A Person",
    method: str | None = None,
    shareable: int = 1,
    owning_campaign_id: str | None = None,
    objected: int = 0,
) -> str:
    values: dict = {
        "company_id": company_id,
        "full_name": name,
        "email": email,
        "email_source_method": method or (patterns.METHOD_WEBSITE if email else None),
        "source": "test",
        "collected_at": utcnow(),
        "shareable": shareable,
        "owning_campaign_id": owning_campaign_id,
        "objected": objected,
    }
    return insert_row("contact", values)


# ---------------------------------------------------------------------------
# The work list: only rows with no address, honouring scope and objections
# ---------------------------------------------------------------------------


def test_missing_email_only_returns_rows_without_an_address() -> None:
    ids = _seed()
    empty = _contact(ids["company_id"], name="No Address")
    filled = _contact(ids["company_id"], name="Has Address", email="has@acme-data.example")

    rows = repo.contacts_missing_email(50, job_seeker_id=ids["seeker_id"])
    ids_seen = {row["id"] for row in rows}
    assert empty in ids_seen and filled not in ids_seen
    # The company fields travel with the row so the pass never re-reads one.
    assert rows[0]["company_name"] == "Acme Data BV"
    assert rows[0]["company_domain"] == "acme-data.example"
    assert repo.contacts_missing_email_count(job_seeker_id=ids["seeker_id"]) == 1


def test_missing_email_excludes_objected_rows() -> None:
    ids = _seed()
    open_row = _contact(ids["company_id"], name="Open")
    _contact(ids["company_id"], name="Blocked", objected=1)

    rows = repo.contacts_missing_email(50, job_seeker_id=ids["seeker_id"])
    assert [row["id"] for row in rows] == [open_row]


def test_missing_email_respects_the_campaign_scope() -> None:
    ids = _seed()
    shared = _contact(ids["company_id"], name="Shared")
    mine = _contact(
        ids["company_id"], name="Mine",
        shareable=0, owning_campaign_id=ids["campaign_id"],
    )
    theirs = _contact(
        ids["company_id"], name="Theirs",
        shareable=0, owning_campaign_id=ids["other_campaign"],
    )

    scoped = {row["id"] for row in repo.contacts_missing_email(50, job_seeker_id=ids["seeker_id"])}
    assert shared in scoped and mine in scoped and theirs not in scoped

    other = {row["id"] for row in repo.contacts_missing_email(50, job_seeker_id=ids["other_id"])}
    assert shared in other and theirs in other and mine not in other

    # ``scope='all'`` (no seeker) is the administrator's whole-table view.
    everything = {row["id"] for row in repo.contacts_missing_email(50)}
    assert {shared, mine, theirs} <= everything


# ---------------------------------------------------------------------------
# The guarded write (FR-303, FR-304, NFR-302)
# ---------------------------------------------------------------------------


def test_set_contact_email_refuses_to_overwrite_and_to_touch_objected() -> None:
    ids = _seed()
    contact_id = _contact(ids["company_id"], name="No Address")
    assert repo.set_contact_email(
        contact_id,
        email="Marie.Dupont@Acme-Data.example",
        method=patterns.METHOD_PATTERN,
        validation_result=validation.UNKNOWN,
        validation_detail={"role": False},
    ) is True
    stored = query_one("SELECT * FROM contact WHERE id = ?", (contact_id,))
    assert stored["email"] == "marie.dupont@acme-data.example"

    # A second write never overwrites an address that is already there.
    assert repo.set_contact_email(
        contact_id,
        email="other@acme-data.example",
        method=patterns.METHOD_PATTERN,
        validation_result=validation.UNKNOWN,
    ) is False
    assert query_one("SELECT email FROM contact WHERE id = ?", (contact_id,))[
        "email"
    ] == "marie.dupont@acme-data.example"

    # An objected row is never given an address (NFR-302).
    blocked = _contact(ids["company_id"], name="Blocked", objected=1)
    assert repo.set_contact_email(
        blocked, email="blocked@acme-data.example",
        method=patterns.METHOD_PATTERN, validation_result=validation.UNKNOWN,
    ) is False
    assert query_one("SELECT email FROM contact WHERE id = ?", (blocked,))["email"] is None


def test_set_contact_email_tracks_uncertainty_and_role() -> None:
    ids = _seed()

    composed = _contact(ids["company_id"], name="Composed")
    repo.set_contact_email(
        composed, email="marie.dupont@acme-data.example", method=patterns.METHOD_PATTERN,
        validation_result=validation.UNKNOWN,
    )
    row = query_one("SELECT * FROM contact WHERE id = ?", (composed,))
    assert row["email_uncertain"] == 1
    assert row["is_generic_mailbox"] == 0

    valid = _contact(ids["company_id"], name="Valid")
    repo.set_contact_email(
        valid, email="jan.peeters@acme-data.example", method=patterns.METHOD_PATTERN,
        validation_result=validation.VALID,
    )
    assert query_one("SELECT email_uncertain FROM contact WHERE id = ?", (valid,))[
        "email_uncertain"
    ] == 0

    # A published address is never uncertain, whatever the verdict.
    published = _contact(ids["company_id"], name="Published")
    repo.set_contact_email(
        published, email="sofie.claes@acme-data.example", method=patterns.METHOD_WEBSITE,
        validation_result=validation.RISKY,
    )
    assert query_one(
        "SELECT email_uncertain FROM contact WHERE id = ?", (published,)
    )["email_uncertain"] == 0

    # A role mailbox is flagged, and stays uncertain as a composed address.
    role = _contact(ids["company_id"], name="Role")
    repo.set_contact_email(
        role, email="jobs@acme-data.example", method=patterns.METHOD_PATTERN,
        validation_result=validation.RISKY, validation_detail={"role": True},
    )
    row = query_one("SELECT * FROM contact WHERE id = ?", (role,))
    assert row["is_generic_mailbox"] == 1 and row["email_uncertain"] == 1


# ---------------------------------------------------------------------------
# The pass (FR-303, FR-304, FR-305)
# ---------------------------------------------------------------------------


def test_backfill_stores_an_inferred_address_and_marks_it_uncertain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = _seed()
    contact_id = _contact(ids["company_id"], name="Marie Dupont")
    monkeypatch.setattr(
        validation, "_resolve_mx",
        lambda domain, timeout=5.0: validation.MXResult(
            has_mx=True, hosts=["mx.acme-data.example"]
        ),
    )

    report = asyncio.run(
        backfill.backfill_missing_emails(ids["seeker_id"], crawl_site=False, allow_smtp=False)
    )
    assert report.considered == 1
    assert report.updated == 1
    assert report.uncertain == 1
    assert report.companies_visited == 1

    row = query_one("SELECT * FROM contact WHERE id = ?", (contact_id,))
    assert row["email"] == "marie.dupont@acme-data.example"
    assert row["email_source_method"] == patterns.METHOD_PATTERN
    assert row["email_validation"] == validation.UNKNOWN
    assert row["email_uncertain"] == 1


def test_backfill_skips_a_candidate_that_validates_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = _seed()
    contact_id = _contact(ids["company_id"], name="Marie Dupont")

    def _invalid(email: str, **kwargs: object) -> validation.ValidationResult:
        return validation.ValidationResult(email=email, result=validation.INVALID, detail={})

    monkeypatch.setattr(validation, "validate", _invalid)

    report = asyncio.run(backfill.backfill_missing_emails(ids["seeker_id"], crawl_site=False))
    assert report.updated == 0
    assert report.skipped_invalid == 1
    assert query_one("SELECT email FROM contact WHERE id = ?", (contact_id,))["email"] is None


def test_backfill_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    ids = _seed()
    _contact(ids["company_id"], name="Marie Dupont")
    monkeypatch.setattr(
        validation, "_resolve_mx",
        lambda domain, timeout=5.0: validation.MXResult(
            has_mx=True, hosts=["mx.acme-data.example"]
        ),
    )

    first = asyncio.run(backfill.backfill_missing_emails(ids["seeker_id"], crawl_site=False))
    assert first.updated == 1

    second = asyncio.run(backfill.backfill_missing_emails(ids["seeker_id"], crawl_site=False))
    assert second.considered == 0
    assert second.updated == 0
    assert second.already_had_email == 0

    missing = repo.contacts_missing_email_count(job_seeker_id=ids["seeker_id"])
    assert missing == 0


def test_backfill_reports_a_row_that_gained_an_address_since_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = _seed()
    filled = _contact(ids["company_id"], name="Already Filled", email="already@acme-data.example")
    monkeypatch.setattr(
        validation, "_resolve_mx",
        lambda domain, timeout=5.0: validation.MXResult(
            has_mx=True, hosts=["mx.acme-data.example"]
        ),
    )

    # Pretend the work list selected the row just before it gained an address:
    # the guarded write must be a no-op and the report must say why.
    row = repo.get_contact(filled)
    row.update(
        {
            "company_name": "Acme Data BV",
            "company_domain": "acme-data.example",
            "company_careers_url": None,
            "company_country": "BE",
        }
    )
    monkeypatch.setattr(repo, "contacts_missing_email", lambda *a, **k: [row])

    report = asyncio.run(backfill.backfill_missing_emails(ids["seeker_id"], crawl_site=False))
    assert report.considered == 1
    assert report.updated == 0
    assert report.already_had_email == 1


# ---------------------------------------------------------------------------
# The endpoint contract
# ---------------------------------------------------------------------------


def test_the_emails_backfill_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    from dreamjob.api.deps import CurrentSeeker, current_admin, current_seeker
    from dreamjob.api.routers import contacts as router_module
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    ids = _seed()
    _contact(ids["company_id"], name="Marie Dupont")

    # Do not leave a background thread behind for the next test's throwaway DB.
    async def _no_start(job_id: str, *args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(router_module.runner, "start", _no_start)

    me = CurrentSeeker(
        id=ids["seeker_id"], email="seeker@example.org", display_name="Stephane",
        is_admin=False, locale="en",
    )
    admin = CurrentSeeker(
        id=ids["seeker_id"], email="seeker@example.org", display_name="Stephane",
        is_admin=True, locale="en",
    )
    app = FastAPI()
    app.include_router(router_module.router, prefix="/api/contacts")
    app.dependency_overrides[current_seeker] = lambda: me
    app.dependency_overrides[current_admin] = lambda: admin

    with TestClient(app) as client:
        missing = client.get("/api/contacts/emails/missing")
        assert missing.status_code == 200
        assert missing.json() == {"count": 1, "scope": "mine"}

        # ``scope=all`` is the administrator's sweep and is refused to a seeker.
        assert client.get(
            "/api/contacts/emails/missing", params={"scope": "all"}
        ).status_code == 403
        assert client.post(
            "/api/contacts/emails/backfill", json={"scope": "all"}
        ).status_code == 403

        started = client.post(
            "/api/contacts/emails/backfill", json={"scope": "mine", "limit": 10}
        )
        assert started.status_code == 202, started.text
        body = started.json()
        assert body["kind"] == "contact_email_backfill"
        assert body["scope"] == "mine" and body["limit"] == 10

        # The options travel in the checkpoint the worker resumes from (NFR-401).
        row = query_one("SELECT checkpoint FROM job_run WHERE id = ?", (body["job_id"],))
        options = from_json(row["checkpoint"], {})["options"]
        assert options["scope"] == "mine" and options["limit"] == 10

        status = client.get(f"/api/contacts/emails/backfill/{body['job_id']}")
        assert status.status_code == 200
        data = status.json()
        assert data["kind"] == "contact_email_backfill"
        assert data["report"] is None

    # An administrator may sweep the whole table.
    app.dependency_overrides[current_seeker] = lambda: admin
    with TestClient(app) as client:
        assert client.post(
            "/api/contacts/emails/backfill", json={"scope": "all", "limit": 5}
        ).status_code == 202


# ---------------------------------------------------------------------------
# NFR-502: the backfill bar moves while the pass works, not once at the end
# ---------------------------------------------------------------------------


def _mx(monkeypatch: pytest.MonkeyPatch) -> None:
    """A mail exchanger for every domain, so no DNS is touched."""
    monkeypatch.setattr(
        validation,
        "_resolve_mx",
        lambda domain, timeout=5.0: validation.MXResult(
            has_mx=True, hosts=["mx.acme-data.example"]
        ),
    )


def test_the_backfill_reports_each_company_as_it_is_visited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A start tick names the company list and every visit moves the bar on."""
    ids = _seed()
    _contact(ids["company_id"], name="Seed Person")
    now = utcnow()
    for index in range(2):
        company_id = insert_row(
            "company",
            {
                "name": f"Acme {index} BV",
                "normalised_name": f"acme {index}",
                "domain": f"acme{index}.example",
                "country": "BE",
                "collected_at": now,
            },
        )
        _contact(company_id, name=f"Person {index}")
    _mx(monkeypatch)

    events: list[dict] = []
    report = asyncio.run(
        backfill.backfill_missing_emails(
            ids["seeker_id"], crawl_site=False, on_progress=events.append
        )
    )

    start = next(event for event in events if event["phase"] == "start")
    assert start["done"] == 0
    assert start["total"] == report.companies_visited == 3
    assert start["report"]["companies_visited"] == 0

    company_events = [event for event in events if event["phase"] == "company"]
    assert [event["done"] for event in company_events] == [1, 2, 3]
    assert company_events[-1]["total"] == 3
    assert len(company_events) == report.companies_visited


def test_the_backfill_reports_a_terminal_done_when_there_is_nothing_to_do(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pass with no contacts still ends 1 / 1 rather than hanging at 0 / 1."""
    ids = _seed()
    _mx(monkeypatch)

    events: list[dict] = []
    report = asyncio.run(
        backfill.backfill_missing_emails(
            ids["seeker_id"], on_progress=events.append
        )
    )
    assert report.companies_visited == 0
    assert [event["phase"] for event in events] == ["done"]
    assert events[0]["done"] == 1 and events[0]["total"] == 1
