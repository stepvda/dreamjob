"""The topbar counters (FR-361, NFR-502).

Four numbers the shell keeps on screen and redraws while a collection runs, so
this file pins three things: the endpoint contract and its authentication, the
definition of each number - above all whose contacts are counted - and the TTL
cache that keeps a polling tab from reaching the database.  Everything runs
against a throwaway SQLite file; no network and no model.
"""

from __future__ import annotations

import base64
import os
import secrets
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest
from dreamjob.config import get_settings
from dreamjob.db.connection import insert_row, utcnow

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


def _seed() -> dict[str, str]:
    """Two seekers, two companies and two vacancies, and three contacts.

    ``Shared`` is visible to both seekers, ``own`` belongs to the first
    seeker's campaign and ``other`` to the second's - so a count that ignores
    scoping is 3 and a correct one is 2 for either seeker.
    """
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
            "collected_at": now,
        },
    )
    second_company = insert_row(
        "company",
        {
            "name": "Beta Analytics BV",
            "normalised_name": "beta analytics",
            "collected_at": now,
        },
    )
    insert_row(
        "vacancy",
        {"title": "Lead Data Engineer", "company_id": company_id, "collected_at": now},
    )
    insert_row(
        "vacancy",
        {"title": "Data Analyst", "company_id": second_company, "collected_at": now},
    )
    insert_row(
        "opportunity",
        {
            "job_seeker_id": seeker_id,
            "campaign_id": campaign_id,
            "company_id": company_id,
            "kind": "vacancy",
            "title": "Lead Data Engineer",
            "created_at": now,
            "updated_at": now,
        },
    )
    insert_row(
        "contact",
        {
            "company_id": company_id,
            "full_name": "Shared Person",
            "email": "shared@acme-data.example",
            "shareable": 1,
            "owning_campaign_id": None,
            "collected_at": now,
        },
    )
    insert_row(
        "contact",
        {
            "company_id": company_id,
            "full_name": "Own Campaign Person",
            "email": "own@acme-data.example",
            "shareable": 0,
            "owning_campaign_id": campaign_id,
            "collected_at": now,
        },
    )
    insert_row(
        "contact",
        {
            "company_id": company_id,
            "full_name": "Other Campaign Person",
            "email": "other@acme-data.example",
            "shareable": 0,
            "owning_campaign_id": other_campaign,
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


def _client_for(seeker_id: str):
    """The overview router with authentication stubbed to one seeker."""
    from dreamjob.api.deps import CurrentSeeker, current_seeker
    from dreamjob.api.routers import overview as router_module
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(router_module.router, prefix="/api/overview")
    app.dependency_overrides[current_seeker] = lambda: CurrentSeeker(
        id=seeker_id,
        email="seeker@example.org",
        display_name="Stephane",
        is_admin=False,
        locale="en",
    )
    return TestClient(app)


def test_the_counters_endpoint_returns_the_four_numbers_for_the_signed_in_seeker() -> None:
    ids = _seed()
    with _client_for(ids["seeker_id"]) as client:
        response = client.get("/api/overview/counters")

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"companies", "jobs", "contacts", "opportunities", "at"}
    assert body["companies"] == 2
    assert body["jobs"] == 2
    assert body["contacts"] == 2  # shared + this seeker's campaign; not the other's
    assert body["opportunities"] == 1
    datetime.fromisoformat(body["at"])  # an ISO-8601 timestamp, not a marker


def test_contacts_and_opportunities_are_scoped_to_the_signed_in_seeker() -> None:
    ids = _seed()
    with _client_for(ids["other_id"]) as client:
        body = client.get("/api/overview/counters").json()

    assert body["contacts"] == 2  # shared + the *other* campaign's contact
    assert body["opportunities"] == 0
    assert body["companies"] == 2 and body["jobs"] == 2  # shared corpus


def test_only_reachable_addresses_count_as_contacts() -> None:
    """Objected, invalid and addressless rows are not contacts (NFR-302, FR-304)."""
    ids = _seed()
    now = utcnow()
    insert_row(
        "contact",
        {
            "company_id": ids["company_id"],
            "full_name": "Objected Person",
            "email": "objected@acme-data.example",
            "objected": 1,
            "shareable": 1,
            "collected_at": now,
        },
    )
    insert_row(
        "contact",
        {
            "company_id": ids["company_id"],
            "full_name": "Invalid Address",
            "email": "invalid@acme-data.example",
            "email_validation": "invalid",
            "shareable": 1,
            "collected_at": now,
        },
    )
    insert_row(
        "contact",
        {
            "company_id": ids["company_id"],
            "full_name": "No Address Yet",
            "email": None,
            "shareable": 1,
            "collected_at": now,
        },
    )

    with _client_for(ids["seeker_id"]) as client:
        body = client.get("/api/overview/counters").json()

    assert body["contacts"] == 2


def test_the_ttl_cache_reuses_a_payload_until_fresh_is_asked() -> None:
    from dreamjob.api.routers import overview as router_module

    ids = _seed()
    with _client_for(ids["seeker_id"]) as client:
        first = client.get("/api/overview/counters")
        assert first.status_code == 200

        insert_row(
            "company",
            {"name": "Gamma BV", "normalised_name": "gamma", "collected_at": utcnow()},
        )
        cached = client.get("/api/overview/counters")
        assert cached.json() == first.json()  # same payload, same "at", no new read

        fresh = client.get("/api/overview/counters", params={"fresh": 1})
        assert fresh.json()["companies"] == first.json()["companies"] + 1

        again = client.get("/api/overview/counters")
        assert again.json() == fresh.json()  # the fresh call refilled the cache

    # The route hands back the stored object itself, not a rebuilt copy.
    assert router_module.counters_for_seeker(ids["seeker_id"]) is router_module.counters_for_seeker(
        ids["seeker_id"]
    )


def test_a_suppressed_company_leaves_the_topbar_counter() -> None:
    """FR-341: hiding a shared company hides it from the counter too."""
    from dreamjob.db.repositories import companies as companies_repo
    from dreamjob.db.repositories import overview

    ids = _seed()
    assert overview.counters(ids["seeker_id"])["companies"] == 2

    companies_repo.suppress_company(ids["company_id"], "hidden for the counter check")
    assert overview.counters(ids["seeker_id"])["companies"] == 1

    companies_repo.unsuppress_company(ids["company_id"])
    assert overview.counters(ids["seeker_id"])["companies"] == 2


def test_the_counters_require_authentication() -> None:
    from dreamjob.api.routers import overview as router_module
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(router_module.router, prefix="/api/overview")
    with TestClient(app) as client:
        assert client.get("/api/overview/counters").status_code == 401
        # ``?fresh=1`` is for the signed-in client; it does not open the route.
        assert client.get("/api/overview/counters", params={"fresh": 1}).status_code == 401


def test_the_combined_statement_agrees_with_each_per_module_counter() -> None:
    """No second definition: the one statement must equal the owning functions.

    ``repositories/overview.py`` embeds the same SQL as the count functions so
    the shell makes one round trip.  This is the guard that the embedding has
    not drifted - the failure mode is a topbar number that disagrees with the
    screen it links to.
    """
    from dreamjob.db.repositories import contacts as contacts_repo
    from dreamjob.db.repositories import knowledge, opportunities, overview

    ids = _seed()
    assert overview.counters(ids["seeker_id"]) == {
        "companies": knowledge.count_companies(),
        "jobs": knowledge.count_vacancies(),
        "contacts": contacts_repo.count_visible_contacts(ids["seeker_id"]),
        "opportunities": opportunities.count_opportunities(ids["seeker_id"]),
    }
