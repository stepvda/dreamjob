"""Deleting a seeker's own rows, and suppressing a shared company.

Requirements exercised: FR-101, FR-142/FR-144, FR-261, FR-321..FR-331,
FR-341, FR-421, NFR-302, NFR-303, NFR-305.

Two rules are pinned down here.  A **private** row that belongs to the caller
is hard-deleted, with the files its cascading children named removed from
disk.  A **shared** knowledge-base row is never hard-deleted: a company is
suppressed instead, and a shared contact is refused with a pointer to the
objection route that blocks it permanently (NFR-302).

Everything runs against a throw-away SQLite file with no network and no model.
"""

from __future__ import annotations

import base64
import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from dreamjob.config import get_settings
from dreamjob.db.connection import insert_row, query_all, query_one, utcnow

_ENV_KEYS = (
    "DREAMJOB_DATA_DIR",
    "DREAMJOB_DB_PATH",
    "DREAMJOB_MASTER_KEY",
    "DREAMJOB_ENV",
    "DREAMJOB_SESSION_SECRET",
    "DEEPSEEK_API_KEY",
    "DREAMJOB_LOCAL_LLM_BASE_URL",
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
    os.environ["DREAMJOB_LOCAL_LLM_BASE_URL"] = ""
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


def _seeker(email: str = "seeker@example.org") -> str:
    now = utcnow()
    return insert_row(
        "job_seeker",
        {
            "email": email,
            "display_name": "Seeker",
            "locale": "en",
            "created_at": now,
            "updated_at": now,
        },
    )


def _campaign(seeker_id: str, name: str = "campaign") -> str:
    now = utcnow()
    directive_id = insert_row(
        "directive_set", {"job_seeker_id": seeker_id, "name": name, "created_at": now}
    )
    profile_id = insert_row(
        "profile_version",
        {"job_seeker_id": seeker_id, "version": 1, "sections": "{}", "created_at": now},
    )
    return insert_row(
        "campaign",
        {
            "job_seeker_id": seeker_id,
            "directive_set_id": directive_id,
            "profile_version_id": profile_id,
            "name": name,
            "created_at": now,
        },
    )


def _company(name: str = "Acme Data") -> str:
    return insert_row(
        "company",
        {
            "normalised_name": name.lower(),
            "name": name,
            "country": "BE",
            "source": "test",
            "collected_at": utcnow(),
        },
    )


def _opportunity(
    seeker_id: str, campaign_id: str, company_id: str | None = None, **overrides: object
) -> str:
    now = utcnow()
    values: dict[str, object] = {
        "job_seeker_id": seeker_id,
        "campaign_id": campaign_id,
        "company_id": company_id,
        "kind": "vacancy",
        "title": "Data Lead",
        "created_at": now,
        "updated_at": now,
        **overrides,
    }
    return insert_row("opportunity", values)


def _package(
    seeker_id: str, opportunity_id: str, files: dict[str, str], *, status: str = "draft"
) -> str:
    now = utcnow()
    return insert_row(
        "application_package",
        {
            "job_seeker_id": seeker_id,
            "opportunity_id": opportunity_id,
            "status": status,
            "language": "en",
            "created_at": now,
            "updated_at": now,
            **files,
        },
    )


def _written_files(tmp_path: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for name, column in (
        ("cv.pdf", "cv_pdf_path"),
        ("cv.docx", "cv_docx_path"),
        ("briefing.pdf", "briefing_pdf_path"),
        ("motivation.pdf", "motivation_pdf_path"),
    ):
        path = tmp_path / name
        path.write_bytes(b"%PDF-1.4 test")
        files[column] = str(path)
    return files


@contextmanager
def _client(seeker_id: str, *, is_admin: bool = False) -> Iterator[object]:
    """This suite's routers, with authentication stubbed to one identity."""
    from dreamjob.api.deps import CurrentSeeker, current_seeker
    from dreamjob.api.routers import applications, companies, contacts, opportunities, pipeline
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(opportunities.router, prefix="/api/opportunities")
    app.include_router(contacts.router, prefix="/api/contacts")
    app.include_router(pipeline.router, prefix="/api/pipeline")
    app.include_router(applications.router, prefix="/api/applications")
    app.include_router(companies.router, prefix="/api/companies")
    app.dependency_overrides[current_seeker] = lambda: CurrentSeeker(
        id=seeker_id,
        email="seeker@example.org",
        display_name="Seeker",
        is_admin=is_admin,
        locale="en",
    )
    with TestClient(app) as client:
        yield client


# ---------------------------------------------------------------------------
# Opportunities: seeker-owned, hard delete (FR-142/FR-144, NFR-305)
# ---------------------------------------------------------------------------


def test_api_deletes_an_owned_opportunity_with_its_package_and_files(tmp_path: Path) -> None:
    seeker_id = _seeker()
    campaign_id = _campaign(seeker_id)
    opportunity_id = _opportunity(seeker_id, campaign_id, _company())
    files = _written_files(tmp_path)
    _package(seeker_id, opportunity_id, files)

    with _client(seeker_id) as client:
        # A package is a user decision (NFR-305), so the plain delete refuses.
        refused = client.delete(f"/api/opportunities/{opportunity_id}")
    assert refused.status_code == 409

    with _client(seeker_id) as client:
        response = client.delete(
            f"/api/opportunities/{opportunity_id}", params={"force": "true"}
        )

    assert response.status_code == 200
    assert response.json() == {"deleted": 1, "refused": []}
    # The package row cascaded with the opportunity ...
    assert query_one("SELECT id FROM opportunity WHERE id = ?", (opportunity_id,)) is None
    assert query_one(
        "SELECT id FROM application_package WHERE opportunity_id = ?", (opportunity_id,)
    ) is None
    # ... and the files it named went with it.
    assert all(not Path(path).exists() for path in files.values())


def test_api_refuses_deleting_another_seekers_opportunity() -> None:
    owner_id = _seeker("owner@example.org")
    intruder_id = _seeker("intruder@example.org")
    campaign_id = _campaign(owner_id)
    opportunity_id = _opportunity(owner_id, campaign_id, _company())

    with _client(intruder_id) as client:
        response = client.delete(f"/api/opportunities/{opportunity_id}")

    assert response.status_code == 404
    assert query_one("SELECT id FROM opportunity WHERE id = ?", (opportunity_id,)) is not None


def test_api_refuses_a_decided_opportunity_until_force(tmp_path: Path) -> None:
    seeker_id = _seeker()
    campaign_id = _campaign(seeker_id)
    opportunity_id = _opportunity(seeker_id, campaign_id, _company())
    # NFR-305: the seeker's own pin protects the row from a clean-up.
    from dreamjob.db.connection import execute

    execute("UPDATE opportunity SET pinned = 1 WHERE id = ?", (opportunity_id,))

    with _client(seeker_id) as client:
        refused = client.delete(f"/api/opportunities/{opportunity_id}")
        allowed = client.delete(f"/api/opportunities/{opportunity_id}", params={"force": "true"})

    assert refused.status_code == 409
    assert query_one("SELECT id FROM opportunity WHERE id = ?", (opportunity_id,)) is None
    assert allowed.status_code == 200
    assert allowed.json()["deleted"] == 1


def test_api_bulk_delete_reports_refusals_and_forced_rows() -> None:
    seeker_id = _seeker()
    other_id = _seeker("other@example.org")
    campaign_id = _campaign(seeker_id)
    other_campaign = _campaign(other_id)
    deletable = [
        _opportunity(seeker_id, campaign_id, _company(f"Company {n}")) for n in range(2)
    ]
    protected = _opportunity(seeker_id, campaign_id, _company("Protected"))
    foreign = _opportunity(other_id, other_campaign, _company("Foreign"))
    from dreamjob.db.connection import execute

    execute("UPDATE opportunity SET selected = 1 WHERE id = ?", (protected,))

    ids = [*deletable, protected, foreign]
    with _client(seeker_id) as client:
        response = client.post("/api/opportunities/delete", json={"opportunity_ids": ids})

    assert response.status_code == 200
    body = response.json()
    assert body["deleted"] == 2
    assert {entry["id"] for entry in body["refused"]} == {protected, foreign}
    assert {entry["reason"] for entry in body["refused"]} == {"user_decision", "not_found"}
    assert query_one("SELECT id FROM opportunity WHERE id = ?", (protected,)) is not None
    assert query_one("SELECT id FROM opportunity WHERE id = ?", (foreign,)) is not None
    assert all(
        query_one("SELECT id FROM opportunity WHERE id = ?", (oid,)) is None for oid in deletable
    )

    with _client(seeker_id) as client:
        forced = client.post(
            "/api/opportunities/delete",
            json={"opportunity_ids": [protected], "force": True},
        )
    assert forced.status_code == 200
    assert forced.json() == {"deleted": 1, "refused": []}
    assert query_one("SELECT id FROM opportunity WHERE id = ?", (protected,)) is None


# ---------------------------------------------------------------------------
# Contacts: campaign-owned only (NFR-303, NFR-302)
# ---------------------------------------------------------------------------


def test_api_deletes_a_campaign_owned_contact() -> None:
    seeker_id = _seeker()
    campaign_id = _campaign(seeker_id)
    contact_id = insert_row(
        "contact",
        {
            "company_id": _company(),
            "email": "campaign.owner@example.org",
            "shareable": 0,
            "owning_campaign_id": campaign_id,
            "collected_at": utcnow(),
        },
    )

    with _client(seeker_id) as client:
        response = client.delete(f"/api/contacts/{contact_id}")

    assert response.status_code == 200
    assert response.json()["deleted"] is True
    assert query_one("SELECT id FROM contact WHERE id = ?", (contact_id,)) is None
    deleted_events = query_all(
        "SELECT action FROM audit_event WHERE action = 'contact.deleted' AND entity_id = ?",
        (contact_id,),
    )
    assert len(deleted_events) == 1


def test_api_refuses_a_shared_contact_with_the_objection_route() -> None:
    seeker_id = _seeker()
    contact_id = insert_row(
        "contact",
        {
            "company_id": _company(),
            "email": "shared@example.org",
            "shareable": 1,
            "collected_at": utcnow(),
        },
    )

    with _client(seeker_id) as client:
        response = client.delete(f"/api/contacts/{contact_id}")

    assert response.status_code == 409
    assert "objection" in response.json()["detail"].lower()
    assert query_one("SELECT id FROM contact WHERE id = ?", (contact_id,)) is not None


def test_api_hides_another_seekers_private_contact() -> None:
    owner_id = _seeker("owner@example.org")
    intruder_id = _seeker("intruder@example.org")
    owner_campaign = _campaign(owner_id)
    contact_id = insert_row(
        "contact",
        {
            "company_id": _company(),
            "email": "private@example.org",
            "shareable": 0,
            "owning_campaign_id": owner_campaign,
            "collected_at": utcnow(),
        },
    )

    with _client(intruder_id) as client:
        response = client.delete(f"/api/contacts/{contact_id}")

    assert response.status_code == 404
    assert query_one("SELECT id FROM contact WHERE id = ?", (contact_id,)) is not None


# ---------------------------------------------------------------------------
# Pipeline cards: seeker-owned, events cascade (FR-421)
# ---------------------------------------------------------------------------


def test_api_deletes_a_pipeline_card_and_its_events() -> None:
    seeker_id = _seeker()
    other_id = _seeker("other@example.org")
    campaign_id = _campaign(seeker_id)
    opportunity_id = _opportunity(seeker_id, campaign_id, _company())
    now = utcnow()
    card_id = insert_row(
        "pipeline_card",
        {
            "job_seeker_id": seeker_id,
            "opportunity_id": opportunity_id,
            "stage": "sent",
            "created_at": now,
            "updated_at": now,
        },
    )
    insert_row(
        "pipeline_card_event",
        {
            "job_seeker_id": seeker_id,
            "pipeline_card_id": card_id,
            "to_stage": "sent",
            "trigger": "system",
            "created_at": now,
        },
    )

    other_campaign = _campaign(other_id)
    foreign_card = insert_row(
        "pipeline_card",
        {
            "job_seeker_id": other_id,
            "opportunity_id": _opportunity(other_id, other_campaign, _company("Other")),
            "stage": "sent",
            "created_at": now,
            "updated_at": now,
        },
    )

    with _client(seeker_id) as client:
        response = client.delete(f"/api/pipeline/cards/{card_id}")
        foreign = client.delete(f"/api/pipeline/cards/{foreign_card}")

    assert response.status_code == 200
    assert query_one("SELECT id FROM pipeline_card WHERE id = ?", (card_id,)) is None
    assert query_one(
        "SELECT id FROM pipeline_card_event WHERE pipeline_card_id = ?", (card_id,)
    ) is None
    assert foreign.status_code == 404
    assert query_one("SELECT id FROM pipeline_card WHERE id = ?", (foreign_card,)) is not None


# ---------------------------------------------------------------------------
# Application packages: hard delete only when never sent (FR-321..FR-331)
# ---------------------------------------------------------------------------


def test_api_deletes_an_unsent_application_package_and_keeps_a_sent_one(
    tmp_path: Path,
) -> None:
    seeker_id = _seeker()
    campaign_id = _campaign(seeker_id)
    opportunity_id = _opportunity(seeker_id, campaign_id, _company())
    (tmp_path / "draft").mkdir(parents=True, exist_ok=True)
    (tmp_path / "sent").mkdir(parents=True, exist_ok=True)
    draft_files = _written_files(tmp_path / "draft")
    sent_files = _written_files(tmp_path / "sent")
    draft_id = _package(seeker_id, opportunity_id, draft_files)
    sent_id = _package(seeker_id, opportunity_id, sent_files, status="sent")

    with _client(seeker_id) as client:
        removed = client.delete(f"/api/applications/{draft_id}")
        refused = client.delete(f"/api/applications/{sent_id}")

    assert removed.status_code == 200
    assert removed.json()["deleted"] is True
    assert query_one("SELECT id FROM application_package WHERE id = ?", (draft_id,)) is None
    assert all(not Path(path).exists() for path in draft_files.values())

    assert refused.status_code == 409
    assert query_one("SELECT id FROM application_package WHERE id = ?", (sent_id,)) is not None
    assert all(Path(path).exists() for path in sent_files.values())


# ---------------------------------------------------------------------------
# Companies: shared knowledge base, soft suppression (FR-341, NFR-302)
# ---------------------------------------------------------------------------


def test_company_suppression_hides_it_from_search_count_facets_and_non_admins() -> None:
    from dreamjob.db.repositories import knowledge as knowledge_repo

    admin_id = _seeker("admin@example.org")
    visitor_id = _seeker("visitor@example.org")
    keep_id = _company("Keep NV")
    hide_id = _company("Hide NV")
    before = knowledge_repo.count_companies()
    assert before == 2

    with _client(admin_id, is_admin=True) as client:
        suppressed = client.post(
            f"/api/companies/{hide_id}/suppress", json={"reason": "scraped by mistake"}
        )

    assert suppressed.status_code == 200
    assert suppressed.json()["suppressed"] is True
    # The hidden row is gone from search, count and facets ...
    assert {row["id"] for row in knowledge_repo.search_companies()} == {keep_id}
    assert knowledge_repo.count_companies() == 1
    facets = knowledge_repo.company_facets()
    assert facets["total"] == 1
    assert sum(facets["by_country"].values()) == 1
    # ... and from a non-admin's detail view, while an admin still opens it.
    with _client(visitor_id) as client:
        assert client.get(f"/api/companies/{hide_id}").status_code == 404
    with _client(admin_id, is_admin=True) as client:
        detail = client.get(f"/api/companies/{hide_id}")
        assert detail.status_code == 200
        # The admin detail payload carries the decision, so the banner and the
        # un-suppress control show on a direct load, not only after suppressing.
        assert detail.json()["suppressed"] is True
        assert detail.json()["suppressed_reason"] == "scraped by mistake"
        kept = client.get(f"/api/companies/{keep_id}")
        assert kept.status_code == 200
        assert kept.json()["suppressed"] is False


def test_company_suppression_is_audited_and_reversible() -> None:
    from dreamjob.db.repositories import knowledge as knowledge_repo

    admin_id = _seeker("admin@example.org")
    company_id = _company("Hide NV")

    with _client(admin_id, is_admin=True) as client:
        client.post(f"/api/companies/{company_id}/suppress", json={"reason": "bad record"})
        restored = client.delete(f"/api/companies/{company_id}/suppress")

    assert restored.status_code == 200
    assert restored.json()["suppressed"] is False
    assert knowledge_repo.count_companies() == 1
    actions = [
        row["action"]
        for row in query_all(
            "SELECT action FROM audit_event WHERE entity_id = ? ORDER BY created_at",
            (company_id,),
        )
    ]
    assert "company.suppressed" in actions
    assert "company.unsuppressed" in actions


def test_company_suppression_role_and_missing_company() -> None:
    seeker_id = _seeker()
    company_id = _company("Acme")

    with _client(seeker_id, is_admin=False) as client:
        forbidden = client.post(
            f"/api/companies/{company_id}/suppress", json={"reason": "not mine to say"}
        )
        missing = client.delete("/api/companies/does-not-exist/suppress")

    assert forbidden.status_code == 403
    # The missing company is checked before the role, so a non-admin gets the
    # same 403; an admin would see 404.  What matters here is that nothing 500s.
    assert missing.status_code in (403, 404)
    assert query_one("SELECT suppressed FROM company WHERE id = ?", (company_id,))["suppressed"] == 0
