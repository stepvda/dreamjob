"""Stored contact certainty, the browse query and the all-companies sweep
(FR-301, FR-303, FR-304, FR-344, NFR-302, NFR-303, NFR-502).

Everything runs against a throwaway SQLite file with no network and no model.
The two rules under test are the ones that decide whether an inferred address
may be trusted (FR-303/FR-304) and who may see a stored contact at all
(FR-344/NFR-303); both live in SQL, so both are exercised against the real
schema rather than a stub.
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
from dreamjob.db.connection import insert_row, query_all, query_one, utcnow
from dreamjob.db.repositories import apply as apply_repo
from dreamjob.db.repositories import contacts as repo
from dreamjob.pipeline import apply_contacts as apply_pipeline
from dreamjob.pipeline import email_patterns as patterns

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
# Fixtures: two seekers, two companies, one opportunity
# ---------------------------------------------------------------------------


def _seed() -> dict[str, str]:
    """Two seekers (so scoping can be told apart) and two companies.

    ``Acme Data BV`` backs the first seeker's opportunity; ``No Vacancy BV``
    has no vacancy and no opportunity, which is the row only the all-companies
    sweep can produce.
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
            "domain": "acme-data.example",
            "country": "BE",
            "careers_url": "https://acme-data.example/jobs",
            "collected_at": now,
        },
    )
    empty_company_id = insert_row(
        "company",
        {
            "name": "No Vacancy BV",
            "normalised_name": "no vacancy",
            "country": "BE",
            "collected_at": now,
        },
    )
    opportunity_id = insert_row(
        "opportunity",
        {
            "job_seeker_id": seeker_id,
            "campaign_id": campaign_id,
            "company_id": company_id,
            "kind": "vacancy",
            "title": "Lead Data Engineer",
            "function_family": "data",
            "selected": 1,
            "language": "en",
            "created_at": now,
            "updated_at": now,
        },
    )
    return {
        "seeker_id": seeker_id,
        "other_id": other_id,
        "campaign_id": campaign_id,
        "other_campaign": other_campaign,
        "company_id": company_id,
        "empty_company_id": empty_company_id,
        "opportunity_id": opportunity_id,
    }


def _add_contact(
    company_id: str,
    email: str,
    *,
    name: str = "A Person",
    method: str = patterns.METHOD_WEBSITE,
    validation: str | None = "unknown",
    uncertain: int | None = None,
    shareable: int = 1,
    owning_campaign_id: str | None = None,
    confidence: float | None = None,
    is_generic: int | None = None,
) -> str:
    values: dict = {
        "company_id": company_id,
        "full_name": name,
        "email": email,
        "email_source_method": method,
        "source": "test",
        "collected_at": utcnow(),
        "shareable": shareable,
        "owning_campaign_id": owning_campaign_id,
    }
    if confidence is not None:
        values["confidence"] = confidence
    if is_generic is not None:
        values["is_generic_mailbox"] = is_generic
    if validation is not None:
        values["email_validation"] = validation
    if uncertain is not None:
        values["email_uncertain"] = uncertain
    contact_id, _ = repo.upsert_contact(values)
    return contact_id


# ---------------------------------------------------------------------------
# FR-303 / FR-304: uncertainty is a stored property
# ---------------------------------------------------------------------------


def test_pattern_inference_without_a_valid_verdict_is_uncertain() -> None:
    ids = _seed()
    contact_id = _add_contact(
        ids["company_id"],
        "marie.dupont@acme-data.example",
        method=patterns.METHOD_PATTERN,
        validation="unknown",
    )
    assert query_one("SELECT email_uncertain FROM contact WHERE id = ?", (contact_id,))[
        "email_uncertain"
    ] == 1

    # No verdict at all is equally uncertain.
    second = _add_contact(
        ids["company_id"],
        "jan.peeters@acme-data.example",
        method=patterns.METHOD_PATTERN,
        validation=None,
    )
    assert query_one("SELECT email_uncertain FROM contact WHERE id = ?", (second,))[
        "email_uncertain"
    ] == 1


def test_a_valid_verdict_clears_uncertainty() -> None:
    ids = _seed()
    contact_id = _add_contact(
        ids["company_id"],
        "marie.dupont@acme-data.example",
        method=patterns.METHOD_PATTERN,
        validation="valid",
    )
    assert query_one("SELECT email_uncertain FROM contact WHERE id = ?", (contact_id,))[
        "email_uncertain"
    ] == 0

    # And a later verdict of ``valid`` clears a flag that was set.
    repo.set_validation(contact_id, "risky")
    assert query_one("SELECT email_uncertain FROM contact WHERE id = ?", (contact_id,))[
        "email_uncertain"
    ] == 1
    repo.set_validation(contact_id, "valid")
    assert query_one("SELECT email_uncertain FROM contact WHERE id = ?", (contact_id,))[
        "email_uncertain"
    ] == 0


def test_a_published_address_is_never_uncertain() -> None:
    ids = _seed()
    contact_id = _add_contact(
        ids["company_id"],
        "jobs@acme-data.example",
        method=patterns.METHOD_WEBSITE,
        validation="risky",
    )
    assert query_one("SELECT email_uncertain FROM contact WHERE id = ?", (contact_id,))[
        "email_uncertain"
    ] == 0
    # ``set_validation`` must not invent uncertainty for a published address.
    repo.set_validation(contact_id, "unknown")
    assert query_one("SELECT email_uncertain FROM contact WHERE id = ?", (contact_id,))[
        "email_uncertain"
    ] == 0


def test_set_validation_tracks_uncertainty_for_inferred_addresses() -> None:
    ids = _seed()
    contact_id = _add_contact(
        ids["company_id"],
        "sofie.claes@acme-data.example",
        method=patterns.METHOD_PATTERN,
        validation="valid",
    )
    repo.set_validation(contact_id, "risky")
    row = query_one("SELECT email_uncertain, email_validation FROM contact WHERE id = ?", (contact_id,))
    assert row["email_validation"] == "risky"
    assert row["email_uncertain"] == 1


# ---------------------------------------------------------------------------
# FR-301 / NFR-502: the browse query
# ---------------------------------------------------------------------------


def test_browse_filters_and_pagination() -> None:
    ids = _seed()
    _add_contact(
        ids["company_id"], "alice@acme-data.example", name="Alice A",
        method=patterns.METHOD_WEBSITE, validation="valid",
    )
    _add_contact(
        ids["company_id"], "bob@acme-data.example", name="Bob B",
        method=patterns.METHOD_PATTERN, validation="unknown",
    )
    _add_contact(
        ids["company_id"], "carol@acme-data.example", name="Carol C",
        method=patterns.METHOD_PATTERN, validation="valid",
    )
    _add_contact(
        ids["empty_company_id"], "dave@no-vacancy.example", name="Dave D",
        method=patterns.METHOD_WEBSITE, validation="risky",
    )

    items, total = repo.browse_contacts(job_seeker_id=ids["seeker_id"], limit=10)
    assert total == 4 and len(items) == 4
    assert all("company_name" in row for row in items)
    assert all("email_uncertain" in row for row in items)

    # q matches the person's name...
    items, total = repo.browse_contacts(q="Alice", job_seeker_id=ids["seeker_id"])
    assert total == 1 and items[0]["full_name"] == "Alice A"
    # ...the address...
    items, total = repo.browse_contacts(q="bob@", job_seeker_id=ids["seeker_id"])
    assert total == 1 and items[0]["email"] == "bob@acme-data.example"
    # ...and the company name.
    items, total = repo.browse_contacts(q="Acme Data", job_seeker_id=ids["seeker_id"])
    assert total == 3

    items, total = repo.browse_contacts(validation="valid", job_seeker_id=ids["seeker_id"])
    assert {row["email"] for row in items} == {"alice@acme-data.example", "carol@acme-data.example"}

    items, total = repo.browse_contacts(
        method=patterns.METHOD_PATTERN, job_seeker_id=ids["seeker_id"]
    )
    assert {row["email"] for row in items} == {"bob@acme-data.example", "carol@acme-data.example"}

    items, total = repo.browse_contacts(uncertain=1, job_seeker_id=ids["seeker_id"])
    assert total == 1 and items[0]["email"] == "bob@acme-data.example"

    items, total = repo.browse_contacts(
        company_id=ids["empty_company_id"], job_seeker_id=ids["seeker_id"]
    )
    assert total == 1 and items[0]["email"] == "dave@no-vacancy.example"

    # Pagination: the total is the whole match, the page is the window.
    first, total = repo.browse_contacts(
        job_seeker_id=ids["seeker_id"], limit=2, offset=0
    )
    second, _ = repo.browse_contacts(job_seeker_id=ids["seeker_id"], limit=2, offset=2)
    assert total == 4 and len(first) == 2 and len(second) == 2
    assert {row["id"] for row in first}.isdisjoint({row["id"] for row in second})


def test_browse_orders_by_company_name() -> None:
    ids = _seed()
    _add_contact(ids["company_id"], "zoe@acme-data.example", name="Zoe Z")
    _add_contact(ids["empty_company_id"], "adam@no-vacancy.example", name="Adam A")
    items, _ = repo.browse_contacts(order="company", job_seeker_id=ids["seeker_id"])
    names = [row["company_name"] for row in items]
    assert names == sorted(names, key=str.lower)


def test_browse_facets_count_beside_the_list() -> None:
    ids = _seed()
    _add_contact(
        ids["company_id"], "alice@acme-data.example",
        method=patterns.METHOD_WEBSITE, validation="valid",
    )
    _add_contact(
        ids["company_id"], "bob@acme-data.example",
        method=patterns.METHOD_PATTERN, validation="unknown",
    )
    facets = repo.browse_facets(job_seeker_id=ids["seeker_id"])
    assert facets["by_validation"]["valid"] == 1
    assert facets["by_validation"]["unknown"] == 1
    assert facets["by_method"][patterns.METHOD_PATTERN] == 1
    assert facets["uncertain"] == 1
    # Filtering to valid still shows how many risky/unknown the query would give.
    filtered = repo.browse_facets(validation="valid", job_seeker_id=ids["seeker_id"])
    assert filtered["by_validation"]["unknown"] == 1


def test_objected_and_invalid_addresses_are_hidden_from_a_seeker() -> None:
    ids = _seed()
    invalid_id = _add_contact(
        ids["company_id"], "invalid@acme-data.example",
        method=patterns.METHOD_WEBSITE, validation="invalid",
    )
    objected_id = _add_contact(
        ids["company_id"], "objected@acme-data.example",
        method=patterns.METHOD_WEBSITE, validation="valid",
    )
    repo.record_objection("objected@acme-data.example", source="unsubscribe")

    items, total = repo.browse_contacts(job_seeker_id=ids["seeker_id"])
    ids_seen = {row["id"] for row in items}
    assert total == 0 and invalid_id not in ids_seen and objected_id not in ids_seen

    # The administrator's view reads ``contact`` and may see both.
    admin_items, admin_total = repo.browse_contacts(include_blocked=True)
    admin_ids = {row["id"] for row in admin_items}
    assert admin_total == 2 and invalid_id in admin_ids and objected_id in admin_ids

    # ``invalid`` is not even reachable as a filter for a seeker.
    _, invalid_total = repo.browse_contacts(
        validation="invalid", job_seeker_id=ids["seeker_id"]
    )
    assert invalid_total == 0


def test_browse_scopes_campaign_rows_to_their_seeker() -> None:
    ids = _seed()
    shared_id = _add_contact(
        ids["company_id"], "shared@acme-data.example",
        method=patterns.METHOD_WEBSITE, validation="valid", shareable=1,
    )
    private_id = _add_contact(
        ids["company_id"], "private@acme-data.example",
        method=patterns.METHOD_WEBSITE, validation="valid",
        shareable=0, owning_campaign_id=ids["campaign_id"],
    )

    items, total = repo.browse_contacts(job_seeker_id=ids["seeker_id"])
    own_ids = {row["id"] for row in items}
    assert total == 2 and shared_id in own_ids and private_id in own_ids

    # The other seeker sees the shared address, not the campaign-scoped one.
    items, total = repo.browse_contacts(job_seeker_id=ids["other_id"])
    other_ids = {row["id"] for row in items}
    assert total == 1 and shared_id in other_ids and private_id not in other_ids

    # An administrator browsing without ``include_blocked`` sees everything.
    _, admin_total = repo.browse_contacts()
    assert admin_total == 2


# ---------------------------------------------------------------------------
# The endpoint contract
# ---------------------------------------------------------------------------


def test_the_browse_endpoint_contract() -> None:
    from dreamjob.api.deps import CurrentSeeker, current_admin, current_seeker
    from dreamjob.api.routers import contacts as router_module
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    ids = _seed()
    _add_contact(
        ids["company_id"], "alice@acme-data.example", name="Alice A",
        method=patterns.METHOD_WEBSITE, validation="valid",
    )
    _add_contact(
        ids["company_id"], "bob@acme-data.example", name="Bob B",
        method=patterns.METHOD_PATTERN, validation="unknown",
    )

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
        body = client.get("/api/contacts", params={"limit": 1, "offset": 0}).json()
        assert set(body) == {"items", "total", "limit", "offset", "facets"}
        assert body["total"] == 2 and body["limit"] == 1 and len(body["items"]) == 1
        assert set(body["facets"]) == {"by_validation", "by_method", "uncertain"}

        filtered = client.get(
            "/api/contacts", params={"uncertain": 1}
        ).json()
        assert filtered["total"] == 1
        assert filtered["items"][0]["email_uncertain"] == 1

        by_order = client.get("/api/contacts", params={"order": "company"}).json()
        assert by_order["total"] == 2

        # A non-administrator cannot ask for the blocked rows.
        assert client.get(
            "/api/contacts", params={"include_blocked": "true"}
        ).status_code == 403

    app.dependency_overrides[current_seeker] = lambda: admin
    with TestClient(app) as client:
        assert client.get(
            "/api/contacts", params={"include_blocked": "true"}
        ).status_code == 200


def test_discover_request_accepts_and_returns_the_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dreamjob.api.deps import CurrentSeeker, current_admin, current_seeker
    from dreamjob.api.routers import contacts as router_module
    from dreamjob.db.connection import from_json
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    ids = _seed()

    # The job is not started: this pins the request contract and the checkpoint
    # the worker would resume from, without leaving a background thread behind
    # for the next test file's throwaway database.
    async def _no_start(job_id: str, *args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(router_module.runner, "start", _no_start)

    admin = CurrentSeeker(
        id=ids["seeker_id"], email="seeker@example.org", display_name="Stephane",
        is_admin=True, locale="en",
    )
    app = FastAPI()
    app.include_router(router_module.router, prefix="/api/contacts")
    app.dependency_overrides[current_seeker] = lambda: admin
    app.dependency_overrides[current_admin] = lambda: admin

    with TestClient(app) as client:
        started = client.post(
            "/api/contacts/discover",
            json={
                "scope": "all",
                "limit": 7,
                "max_companies": 3,
                "crawl_site": False,
                "derive_domains": False,
            },
        )
        assert started.status_code == 202, started.text
        body = started.json()
        assert body["scope"] == "all"
        assert body["limit"] == 7

        # ``scope`` and ``max_companies`` travel in the checkpoint, which is
        # what the worker passes to ``ensure_apply_contacts`` (FR-185/NFR-401).
        row = query_one("SELECT checkpoint FROM job_run WHERE id = ?", (body["job_id"],))
        options = from_json(row["checkpoint"], {})["options"]
        assert options["scope"] == "all"
        assert options["max_companies"] == 3

        # ``max_companies`` outside the schema bound is refused before any job.
        assert client.post(
            "/api/contacts/discover", json={"scope": "all", "max_companies": 0}
        ).status_code == 422


# ---------------------------------------------------------------------------
# FR-301: the all-companies sweep pool and scope
# ---------------------------------------------------------------------------


def test_all_companies_for_contact_includes_a_company_with_no_vacancy() -> None:
    ids = _seed()
    rows = apply_repo.all_companies_for_contact(50)
    by_id = {row["company_id"]: row for row in rows}
    assert ids["empty_company_id"] in by_id
    empty = by_id[ids["empty_company_id"]]
    assert empty["company_name"] == "No Vacancy BV"
    assert empty["vacancy_count"] == 0
    assert empty["has_contact"] == 0
    assert set(empty) >= {
        "company_id", "company_name", "company_domain", "careers_url",
        "company_country", "vacancy_count", "latest_vacancy_at",
        "backs_opportunity", "has_contact", "resolution_status", "resolved_at",
    }


def test_all_companies_for_contact_marks_a_reachable_company() -> None:
    ids = _seed()
    _add_contact(
        ids["company_id"], "hr@acme-data.example",
        method=patterns.METHOD_WEBSITE, validation="valid",
    )
    rows = apply_repo.all_companies_for_contact(
        50, job_seeker_id=ids["seeker_id"], include_covered=True
    )
    by_id = {row["company_id"]: row for row in rows}
    assert by_id[ids["company_id"]]["has_contact"] == 1
    assert by_id[ids["company_id"]]["backs_opportunity"] == 1


def test_all_companies_for_contact_excludes_a_company_with_a_usable_contact() -> None:
    """``scope='all'`` means "no contact yet", not "no resolution row yet"."""
    ids = _seed()
    _add_contact(
        ids["company_id"], "hr@acme-data.example",
        method=patterns.METHOD_WEBSITE, validation="valid",
    )
    by_default = {row["company_id"] for row in apply_repo.all_companies_for_contact(50)}
    assert ids["company_id"] not in by_default
    assert ids["empty_company_id"] in by_default

    refreshed = {
        row["company_id"]: row
        for row in apply_repo.all_companies_for_contact(50, include_covered=True)
    }
    assert refreshed[ids["company_id"]]["has_contact"] == 1


def test_all_companies_for_contact_keeps_a_resolved_but_unreachable_company() -> None:
    """A company can never resolve and must stay in the pool forever.

    The ATS-vendor domains ``usable_employer_domain`` refuses carry exactly this
    verdict; filtering on resolution freshness collapsed the pool to those
    unreachable rows, so the selection has to be on the contact instead.
    """
    ids = _seed()
    apply_repo.record_resolution(
        ids["company_id"],
        {
            "status": "unreachable",
            "domain": "acme-data.example",
            "reason": "no address survived FR-304",
        },
    )
    by_id = {row["company_id"]: row for row in apply_repo.all_companies_for_contact(50)}
    assert ids["company_id"] in by_id
    assert by_id[ids["company_id"]]["resolution_status"] == "unreachable"
    assert by_id[ids["company_id"]]["has_contact"] == 0


def test_scope_all_visits_every_company_without_early_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = _seed()
    seen: list[str] = []

    async def _fake_resolve(company: dict, **kwargs: object) -> apply_pipeline.CompanyOutcome:
        seen.append(company["company_id"])
        return apply_pipeline.CompanyOutcome(
            company_id=company["company_id"],
            company_name=company["company_name"],
            vacancy_count=int(company.get("vacancy_count") or 0),
            status="reachable",
            email=f"hr@{company['company_id']}.example",
            method=patterns.METHOD_WEBSITE,
            validation="valid",
        )

    monkeypatch.setattr(apply_pipeline, "resolve_company", _fake_resolve)
    report = asyncio.run(
        apply_pipeline.ensure_apply_contacts(
            ids["seeker_id"], limit=50, scope="all", crawl_site=False, derive_domains=False
        )
    )
    assert report.scope == "all"
    assert report.as_dict()["scope"] == "all"
    assert report.requested == len(seen)
    # The zero-vacancy company was visited: ``all`` does not stop on coverage.
    assert set(seen) == {ids["company_id"], ids["empty_company_id"]}


# ---------------------------------------------------------------------------
# FR-303/FR-304: the materialised coverage figures match a brute-force scan
# ---------------------------------------------------------------------------


def _add_vacancy(company_id: str, title: str) -> str:
    return insert_row(
        "vacancy",
        {"company_id": company_id, "title": title, "collected_at": utcnow()},
    )


def _new_company(name: str) -> str:
    return insert_row(
        "company",
        {
            "name": name,
            "normalised_name": name.lower(),
            "country": "BE",
            "collected_at": utcnow(),
        },
    )


def _brute_force_coverage() -> tuple[int, int, dict[str, dict]]:
    """Recompute both figures row by row, with no correlated SQL.

    ``usable_contact`` stays the source of truth for what counts as an address
    (NFR-302, FR-304); the point of the test is that the per-company ranking and
    the ``vacancy`` join produce exactly what a plain Python pass produces.
    """
    vacancies = query_all("SELECT id, company_id FROM vacancy WHERE company_id IS NOT NULL")
    usable = query_all(
        "SELECT id, company_id, email_source_method, email_validation, "
        "       is_generic_mailbox, confidence "
        "  FROM usable_contact "
        " WHERE email IS NOT NULL AND email <> '' AND company_id IS NOT NULL"
    )
    covered = {row["company_id"] for row in usable}
    with_contact = sum(1 for row in vacancies if row["company_id"] in covered)

    best: dict[str, dict] = {}
    for row in usable:
        current = best.get(row["company_id"])
        if current is None or float(row["confidence"]) > float(current["confidence"]):
            best[row["company_id"]] = row

    grouped: dict[tuple[str, str], dict] = {}
    for vacancy in vacancies:
        contact = best.get(vacancy["company_id"])
        if contact is None:
            continue
        method = contact["email_source_method"] or "unknown"
        if method == "pattern_inference" and contact["is_generic_mailbox"] == 1:
            method = "conventional_mailbox"
        validation = contact["email_validation"] or "unknown"
        entry = grouped.setdefault((method, validation), {"vacancies": 0, "companies": set()})
        entry["vacancies"] += 1
        entry["companies"].add(contact["company_id"])

    by_method: dict[str, dict] = {}
    for (method, validation), entry in grouped.items():
        bucket = by_method.setdefault(
            method, {"vacancies": 0, "companies": 0, "by_validation": {}}
        )
        bucket["vacancies"] += entry["vacancies"]
        bucket["companies"] += len(entry["companies"])
        bucket["by_validation"][validation] = entry["vacancies"]

    return len(vacancies), with_contact, by_method


def test_materialised_coverage_matches_a_brute_force_scan() -> None:
    ids = _seed()

    # Acme: two vacancies share one published contact, and it also carries an
    # objecting and an invalid address that ``usable_contact`` must exclude.
    _add_vacancy(ids["company_id"], "Data Engineer")
    _add_vacancy(ids["company_id"], "Analyst")
    _add_contact(
        ids["company_id"], "hr@acme-data.example",
        method=patterns.METHOD_WEBSITE, validation="valid", confidence=0.8,
    )
    _add_contact(
        ids["company_id"], "objected@acme-data.example",
        method=patterns.METHOD_WEBSITE, validation="valid",
    )
    repo.record_objection("objected@acme-data.example", source="unsubscribe")
    _add_contact(
        ids["company_id"], "invalid@acme-data.example",
        method=patterns.METHOD_WEBSITE, validation="invalid",
    )

    # Two Vac: two vacancies but only an objecting address -> no usable contact.
    two_vac = _new_company("Two Vac BV")
    _add_vacancy(two_vac, "First")
    _add_vacancy(two_vac, "Second")
    _add_contact(
        two_vac, "blocked@two-vac.example",
        method=patterns.METHOD_WEBSITE, validation="valid",
    )
    repo.record_objection("blocked@two-vac.example", source="unsubscribe")

    # Ranked: two usable contacts; the higher-confidence one decides the bucket,
    # so the generic inferred address must not surface.
    ranked = _new_company("Ranked BV")
    _add_vacancy(ranked, "Only")
    _add_contact(
        ranked, "info@ranked.example",
        method=patterns.METHOD_PATTERN, validation="unknown",
        confidence=0.3, is_generic=1,
    )
    _add_contact(
        ranked, "jobs@ranked.example",
        method=patterns.METHOD_WEBSITE, validation="valid", confidence=0.9,
    )

    # Generic: the inferred generic mailbox is the only address, and it buckets
    # as a conventional mailbox rather than a published one.
    generic = _new_company("Generic BV")
    _add_vacancy(generic, "Uncovered")
    _add_contact(
        generic, "info@generic.example",
        method=patterns.METHOD_PATTERN, validation="unknown", is_generic=1,
    )

    # No Contact: a vacancy with nobody to write to.
    no_contact = _new_company("No Contact BV")
    _add_vacancy(no_contact, "Nobody")

    # The seeded company with no vacancy still contributes a usable contact,
    # which must not move the vacancy coverage figures.
    _add_contact(
        ids["empty_company_id"], "dave@no-vacancy.example",
        method=patterns.METHOD_WEBSITE, validation="risky",
    )

    total, with_contact, by_method = _brute_force_coverage()
    assert (total, with_contact) == (7, 4)

    assert apply_repo.vacancies_with_contact() == {
        "vacancies": total,
        "with_contact": with_contact,
    }

    coverage = apply_repo.coverage_by_method()
    assert coverage["vacancies"] == total
    assert coverage["vacancies_with_contact"] == with_contact
    assert coverage["by_method"] == by_method

    # Acme's two and Ranked's one make the published bucket; Ranked's generic
    # contact was outranked, so no conventional mailbox appears for it.
    assert coverage["by_method"]["website"]["vacancies"] == 3
    assert coverage["by_method"]["website"]["by_validation"]["valid"] == 3
    assert coverage["by_method"]["website"]["companies"] == 2
    assert coverage["by_method"]["conventional_mailbox"]["vacancies"] == 1
    assert coverage["by_method"]["conventional_mailbox"]["by_validation"]["unknown"] == 1
