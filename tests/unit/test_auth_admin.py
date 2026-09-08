"""Authentication and administration slice (NFR-202, FR-108, CR-410, FR-361..364, IR-101).

Runs entirely against a temporary SQLite file; no network and no LLM calls.
"""

from __future__ import annotations

import base64
import os
import secrets
from collections.abc import Iterator
from pathlib import Path

import pyotp
import pytest
from dreamjob.config import get_settings
from fastapi import FastAPI
from fastapi.testclient import TestClient

_ENV_KEYS = (
    "DREAMJOB_DATA_DIR",
    "DREAMJOB_DB_PATH",
    "DREAMJOB_MASTER_KEY",
    "DREAMJOB_SESSION_SECRET",
    "DREAMJOB_ENV",
)


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path) -> Iterator[None]:
    """Point the whole stack at a throwaway database for the duration of a test."""
    saved = {k: os.environ.get(k) for k in _ENV_KEYS}
    os.environ["DREAMJOB_DATA_DIR"] = str(tmp_path / "data")
    os.environ["DREAMJOB_DB_PATH"] = str(tmp_path / "data" / "test.db")
    os.environ["DREAMJOB_MASTER_KEY"] = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    os.environ["DREAMJOB_SESSION_SECRET"] = secrets.token_urlsafe(32)
    os.environ["DREAMJOB_ENV"] = "development"
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


@pytest.fixture
def client() -> Iterator[TestClient]:
    """Only this slice's routers, so the test does not depend on the other slices."""
    from dreamjob.api.routers import admin as admin_router
    from dreamjob.api.routers import auth as auth_router

    app = FastAPI()
    app.include_router(auth_router.router, prefix="/api/auth")
    app.include_router(admin_router.router, prefix="/api/admin")
    with TestClient(app) as c:
        yield c


PASSWORD = "Str0ng-Passphrase!2026"


def _register(client: TestClient, email: str = "owner@example.com") -> dict:
    resp = client.post(
        "/api/auth/register",
        json={"email": email, "display_name": "Test Owner", "password": PASSWORD},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Authentication (NFR-202)
# ---------------------------------------------------------------------------


def test_register_first_account_is_admin_and_me_works(client: TestClient) -> None:
    body = _register(client)
    assert body["is_admin"] is True  # first account bootstraps the admin role

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == "owner@example.com"
    assert me.json()["mfa"]["active"] is False

    assert client.post("/api/auth/logout").json()["ended"] is True
    assert client.get("/api/auth/me").status_code == 401


def test_weak_password_is_refused(client: TestClient) -> None:
    resp = client.post(
        "/api/auth/register",
        json={"email": "weak@example.com", "display_name": "Weak", "password": "aaaaaaaaaaaa"},
    )
    assert resp.status_code == 422


def test_session_is_bound_to_the_client(client: TestClient) -> None:
    """NFR-202: a stolen cookie replayed from another client is refused."""
    body = _register(client)
    token = body["session"]["token"]

    ok = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert ok.status_code == 200

    client.cookies.clear()
    moved = client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {token}", "user-agent": "some-other-browser/1.0"},
    )
    assert moved.status_code == 401
    assert "binding" in moved.json()["detail"].lower()


def test_totp_enrolment_then_login_requires_a_code(client: TestClient) -> None:
    _register(client)
    enrol = client.post("/api/auth/me/mfa/enroll").json()
    assert enrol["otpauth_uri"].startswith("otpauth://totp/")

    totp = pyotp.TOTP(enrol["secret"])
    assert client.post("/api/auth/me/mfa/activate", json={"code": "000000"}).status_code == 400
    activated = client.post("/api/auth/me/mfa/activate", json={"code": totp.now()})
    assert activated.status_code == 200
    assert activated.json()["active"] is True

    client.post("/api/auth/logout")
    without = client.post(
        "/api/auth/login", json={"email": "owner@example.com", "password": PASSWORD}
    )
    assert without.status_code == 401
    assert without.json()["detail"]["error"] == "mfa_required"

    with_code = client.post(
        "/api/auth/login",
        json={"email": "owner@example.com", "password": PASSWORD, "totp_code": totp.now()},
    )
    assert with_code.status_code == 200


# ---------------------------------------------------------------------------
# Consent (CR-410, CR-401, FR-126)
# ---------------------------------------------------------------------------


def test_llm_transfer_consent_gates_profile_egress(client: TestClient) -> None:
    from dreamjob.security import auth_service as auth

    body = _register(client)
    seeker_id = body["id"]

    assert auth.has_consent(seeker_id, "llm_transfer") is False
    with pytest.raises(auth.ConsentRequired):
        auth.require_consent(seeker_id, "llm_transfer")

    granted = client.post(
        "/api/auth/consent",
        json={"kind": "llm_transfer", "granted": True, "detail": "I agreed to nothing"},
    )
    assert granted.status_code == 200
    assert auth.has_consent(seeker_id, "llm_transfer") is True
    auth.require_consent(seeker_id, "llm_transfer")

    # CR-410: what is recorded is the text this system presented; a note from
    # the job seeker is kept beside it and cannot take its place.
    assert granted.json()["detail"].startswith(auth.CONSENT_KINDS["llm_transfer"])
    assert "I agreed to nothing" in granted.json()["detail"]

    # Withdrawal is recorded as a new decision; the history keeps both.
    client.post("/api/auth/consent", json={"kind": "llm_transfer", "granted": False})
    assert auth.has_consent(seeker_id, "llm_transfer") is False
    assert len(client.get("/api/auth/consent/history").json()) == 2

    bad = client.post("/api/auth/consent", json={"kind": "nonsense", "granted": True})
    assert bad.status_code == 422


# ---------------------------------------------------------------------------
# Fixtures for the data-heavy tests
# ---------------------------------------------------------------------------


def _seed_campaign(seeker_id: str) -> dict[str, str]:
    """A minimal campaign with one shared company and one private opportunity."""
    from dreamjob.db.connection import insert_row, utcnow

    now = utcnow()
    company_id = insert_row(
        "company",
        {"normalised_name": "acme", "name": "Acme NV", "collected_at": now, "domain": "acme.be"},
    )
    profile_version_id = insert_row(
        "profile_version",
        {"job_seeker_id": seeker_id, "version": 1, "sections": {"summary": "x"}, "created_at": now},
    )
    directive_set_id = insert_row(
        "directive_set", {"job_seeker_id": seeker_id, "name": "default", "created_at": now}
    )
    campaign_id = insert_row(
        "campaign",
        {
            "job_seeker_id": seeker_id,
            "directive_set_id": directive_set_id,
            "profile_version_id": profile_version_id,
            "name": "First run",
            "status": "running",
            "stage": "collection",
            "token_budget": 1000,
            "tokens_used": 250,
            "cost_eur": 0.75,
            "started_at": "2026-09-08T09:00:00+00:00",
            "created_at": now,
        },
    )
    insert_row(
        "source_plan_item",
        {
            "campaign_id": campaign_id,
            "adapter_key": "demo_board",
            "status": "done",
            "records_collected": 12,
            "error_count": 1,
            "last_error": "one page timed out",
            "estimated_pages": 3,
            "created_at": now,
        },
    )
    insert_row(
        "job_run",
        {
            "job_seeker_id": seeker_id,
            "campaign_id": campaign_id,
            "kind": "collection",
            "adapter_key": "demo_board",
            "status": "running",
            "progress_done": 5,
            "progress_total": 10,
            "error_count": 1,
            "last_error": "one page timed out",
            "started_at": "2026-09-08T09:00:00+00:00",
            "created_at": now,
        },
    )
    insert_row(
        "llm_call",
        {
            "job_seeker_id": seeker_id,
            "campaign_id": campaign_id,
            "task": "extract.vacancy",
            "model": "deepseek-chat",
            "provider": "deepseek",
            "prompt_text": "personal data in the prompt",
            "response_text": "personal data in the response",
            "input_tokens": 200,
            "output_tokens": 50,
            "cost_eur": 0.75,
            "latency_ms": 900,
            "created_at": "2020-01-01T00:00:00+00:00",
        },
    )
    opportunity_id = insert_row(
        "opportunity",
        {
            "job_seeker_id": seeker_id,
            "campaign_id": campaign_id,
            "company_id": company_id,
            "kind": "vacancy",
            "title": "Head of Data",
            "created_at": now,
            "updated_at": now,
        },
    )
    return {"company_id": company_id, "campaign_id": campaign_id, "opportunity_id": opportunity_id}


# ---------------------------------------------------------------------------
# Export and erasure (NFR-301, FR-108)
# ---------------------------------------------------------------------------


def test_export_returns_private_rows_only(client: TestClient) -> None:
    seeker_id = _register(client)["id"]
    ids = _seed_campaign(seeker_id)

    export = client.get("/api/auth/me/export").json()
    assert export["job_seeker"]["email"] == "owner@example.com"
    assert export["job_seeker"]["password_hash"] == "<redacted>"
    assert [r["id"] for r in export["tables"]["opportunity"]] == [ids["opportunity_id"]]
    assert len(export["tables"]["source_plan_item"]) == 1
    assert "company" not in export["tables"] or all(
        r.get("id") != ids["company_id"] for r in export["tables"]["company"]
    )


def test_erasure_removes_private_rows_and_keeps_the_knowledge_base(client: TestClient) -> None:
    """FR-108: private data goes, shared market data stays."""
    from dreamjob.db.connection import query_one

    seeker_id = _register(client)["id"]
    ids = _seed_campaign(seeker_id)

    resp = client.delete("/api/auth/me")
    assert resp.status_code == 200
    counts = resp.json()["rows_deleted"]
    assert counts["opportunity"] == 1
    assert counts["campaign"] == 1
    assert counts["source_plan_item"] == 1
    assert counts["llm_call"] == 1
    assert counts["job_seeker"] == 1

    for table in ("opportunity", "campaign", "profile_version", "directive_set", "session"):
        assert query_one(f"SELECT COUNT(*) AS n FROM {table}")["n"] == 0
    assert query_one("SELECT id FROM company WHERE id = ?", (ids["company_id"],)) is not None

    # An anonymous marker proves the erasure happened without naming the person.
    marker = query_one("SELECT * FROM audit_event WHERE action = 'job_seeker.erased'")
    assert marker is not None and marker["job_seeker_id"] is None
    assert seeker_id not in (marker["detail"] or "")


# ---------------------------------------------------------------------------
# Administration (FR-361..364, IR-101, NFR-702)
# ---------------------------------------------------------------------------


def test_campaign_dashboard_reports_progress_records_errors_and_cost(client: TestClient) -> None:
    seeker_id = _register(client)["id"]
    ids = _seed_campaign(seeker_id)

    body = client.get(f"/api/admin/campaigns/{ids['campaign_id']}/dashboard").json()
    assert body["campaign"]["stage"] == "collection"
    assert body["records_total"] == 12
    assert body["sources"][0]["adapter_key"] == "demo_board"
    assert body["tokens"] == {
        **body["tokens"],
        "budget": 1000,
        "used": 250,
        "remaining": 750,
        "input_tokens": 200,
        "output_tokens": 50,
        "calls": 1,
    }
    assert body["cost_eur"] == 0.75
    assert body["timing"]["progress_fraction"] == 0.5
    assert body["timing"]["estimated_remaining_seconds"] is not None
    assert any(e["count"] for e in body["errors"])


def test_dashboard_of_another_seeker_is_not_reachable(client: TestClient) -> None:
    """FR-344: campaign data is private to its job seeker."""
    owner_id = _register(client)["id"]
    ids = _seed_campaign(owner_id)
    client.post("/api/auth/logout")

    other = client.post(
        "/api/auth/register",
        json={"email": "other@example.com", "display_name": "Other", "password": PASSWORD},
    )
    assert other.status_code == 201
    assert other.json()["is_admin"] is False
    assert client.get(f"/api/admin/campaigns/{ids['campaign_id']}/dashboard").status_code == 404
    assert client.get("/api/admin/overview").status_code == 403


def test_prohibited_source_needs_acknowledgement_before_it_can_be_enabled(
    client: TestClient,
) -> None:
    """IR-101: 'terms prohibit automated access' is a gate, not a label."""
    from dreamjob.db.connection import upsert_row, utcnow

    _register(client)
    upsert_row(
        "source_catalogue",
        {
            "adapter_key": "linkedin",
            "display_name": "LinkedIn",
            "source_type": "linkedin",
            "access_method": "browser",
            "tos_status": "prohibited",
            "legal_notes": "CR-401: automated access is prohibited by the user agreement",
            "enabled": 0,
            "requires_ack": 1,
            "updated_at": utcnow(),
        },
        ["adapter_key"],
    )

    blocked = client.patch("/api/admin/sources/linkedin", json={"enabled": True})
    assert blocked.status_code == 409

    acked = client.post(
        "/api/admin/sources/linkedin/acknowledge", json={"accepted": True, "note": "owner accepts"}
    )
    assert acked.status_code == 200
    assert acked.json()["acknowledged_at"]

    enabled = client.patch(
        "/api/admin/sources/linkedin",
        json={"enabled": True, "rate_limit_rps": 0.2, "max_pages": 40},
    )
    assert enabled.status_code == 200
    assert enabled.json()["effective_enabled"] is True
    assert enabled.json()["caps"] == {"max_pages": 40}
    assert enabled.json()["rate_limit_rps"] == 0.2

    revoked = client.delete("/api/admin/sources/linkedin/acknowledge")
    assert revoked.json()["effective_enabled"] is False

    trail = client.get("/api/admin/audit", params={"entity_id": "linkedin"}).json()
    assert {e["action"] for e in trail} >= {
        "admin.source_acknowledged",
        "admin.source_configured",
        "admin.source_acknowledgement_revoked",
    }


def test_llm_config_override_and_log_redaction(client: TestClient) -> None:
    """FR-362 configuration persists; FR-364 retention nulls prompt and response."""
    from dreamjob.db.connection import query_one

    seeker_id = _register(client)["id"]
    _seed_campaign(seeker_id)

    put = client.put(
        "/api/admin/llm-config",
        json={
            "model_strong": "deepseek-reasoner",
            "task_models": {"extract.vacancy": "deepseek-chat"},
            "default_token_budget": 500_000,
            "log_retention_days": 7,
        },
    )
    assert put.status_code == 200
    effective = client.get("/api/admin/llm-config").json()["effective"]
    assert effective["default_token_budget"] == 500_000
    assert effective["task_models"]["extract.vacancy"] == "deepseek-chat"
    assert effective["log_retention_days"] == 7

    calls = client.get("/api/admin/llm-calls").json()
    assert calls["total"] == 1
    assert "prompt_text" not in calls["items"][0]  # listings never carry the text
    assert calls["items"][0]["has_prompt_text"] == 1

    result = client.post("/api/admin/llm-calls/redact", json={"older_than_days": 30}).json()
    assert result["redacted"] == 1
    row = query_one("SELECT prompt_text, response_text, redacted_at, cost_eur FROM llm_call")
    assert row["prompt_text"] is None and row["response_text"] is None
    assert row["redacted_at"] is not None
    assert row["cost_eur"] == 0.75  # counters survive the redaction

    again = client.post("/api/admin/llm-calls/redact", json={"older_than_days": 30})
    assert again.json()["redacted"] == 0


def test_audit_trail_is_append_only_and_records_the_actor(client: TestClient) -> None:
    """NFR-702: approvals and sends leave a row that no route can edit."""
    from dreamjob.security.audit import record_audit, trail_for_entity

    seeker_id = _register(client)["id"]
    event_id = record_audit(
        "application.approved",
        "application_package",
        "pkg-1",
        seeker_id=seeker_id,
        detail={"profile_version_id": "pv-1", "approved_by": seeker_id},
    )
    assert event_id

    trail = trail_for_entity("application_package", "pkg-1")
    assert trail[0]["action"] == "application.approved"
    assert trail[0]["actor"] == seeker_id
    assert "pv-1" in trail[0]["detail"]

    routes = {
        (method, route.path)
        for route in client.app.routes
        for method in getattr(route, "methods", set())
    }
    assert not [r for r in routes if r[1].endswith("/audit") and r[0] not in {"GET", "HEAD"}]


def test_create_seeker_helper_isolates_accounts(client: TestClient) -> None:
    """FR-101: two accounts, two private spaces."""
    from dreamjob.db.repositories import seekers as repo

    a = repo.create_seeker("a@example.com", "A")
    b = repo.create_seeker("b@example.com", "B")
    assert a != b
    _seed_campaign(a)

    export_a = repo.export_private_data(a)
    export_b = repo.export_private_data(b)
    assert len(export_a["tables"]["campaign"]) == 1
    assert export_b["tables"]["campaign"] == []

    with pytest.raises(repo.EmailAlreadyRegistered):
        repo.create_seeker("a@example.com", "Duplicate")


def test_a_private_table_added_by_a_later_migration_is_exported_and_erased(
    client: TestClient,
) -> None:
    """FR-108, NFR-301: coverage is read from the schema, not from a fixed list.

    Stands in for the tables the other slices' migrations keep adding: nothing
    in this slice names ``later_private_thing``, yet the export must contain
    its row and the erasure must delete both the row and the file it points at.
    """
    from dreamjob.db.connection import execute, insert_row, query_one

    seeker_id = _register(client)["id"]
    document = get_settings().generated_dir / "later-private-thing.txt"
    document.parent.mkdir(parents=True, exist_ok=True)
    document.write_text("private", encoding="utf-8")

    execute(
        "CREATE TABLE later_private_thing ("
        "  id TEXT PRIMARY KEY,"
        "  job_seeker_id TEXT NOT NULL REFERENCES job_seeker(id) ON DELETE CASCADE,"
        "  doc_path TEXT)"
    )
    insert_row("later_private_thing", {"job_seeker_id": seeker_id, "doc_path": str(document)})

    export = client.get("/api/auth/me/export").json()
    assert len(export["tables"]["later_private_thing"]) == 1

    counts = client.delete("/api/auth/me").json()["rows_deleted"]
    assert counts["later_private_thing"] == 1
    assert counts["files_deleted"] == 1
    assert not document.exists()
    assert query_one("SELECT COUNT(*) AS n FROM later_private_thing")["n"] == 0


def test_private_table_discovery_is_deletion_safe() -> None:
    """The discovered order deletes a child before the row it references."""
    from dreamjob.db.repositories import seekers as repo

    tables = repo.private_tables()
    position = {table: index for index, table in enumerate(tables)}
    for table in tables:
        for referenced in repo._referenced_tables(table):
            if referenced in position and referenced != table:
                assert position[referenced] > position[table], (table, referenced)
    assert "field_path" not in {column for _, column in repo.private_file_columns()}
