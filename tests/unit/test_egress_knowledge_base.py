"""Egress provenance, robots.txt honesty and knowledge-base writes.

These are the three ways a retrieval run used to lie about itself:

* a page served from ``http_cache`` came back with ``raw_document_id=None``, so
  the FR-183 link between a stored row and the document it came from was NULL
  on every repeat campaign;
* a robots.txt that could not be *read* was cached as a robots.txt that says
  *nothing*, which switched FR-182 off for that domain for a day;
* the knowledge-base writer returned ``None`` for a record it refused or could
  not store, and nothing counted those, so a plan item whose every record was
  dropped reported "done, 0 records, 0 errors".

Nothing here touches the network.  The HTTP tests drive the real
:class:`EgressClient` through an ``httpx.MockTransport``, and the writer tests
run a *captured* Greenhouse response (``tests/fixtures/knowledge_base``,
boards-api.greenhouse.io/v1/boards/collibra/jobs?content=true, 39 postings,
captured 2026-09-09) through the real adapter and the real writer.
"""

from __future__ import annotations

import asyncio
import base64
import os
import secrets
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from dreamjob.adapters.ats.greenhouse import GreenhouseAdapter
from dreamjob.adapters.base import RawRecord
from dreamjob.config import get_settings
from dreamjob.db.connection import query_all, query_one, utcnow
from dreamjob.egress.client import (
    ROBOTS_FAILURE_TTL_SECONDS,
    DomainLimiter,
    EgressClient,
    RobotsDisallowed,
    RobotsUnavailable,
)
from dreamjob.pipeline import dedup
from dreamjob.pipeline.knowledge_base import KnowledgeBaseWriter

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "knowledge_base"

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
# Offline HTTP plumbing
# ---------------------------------------------------------------------------

PAGE = b"<html><body><h1>Careers</h1></body></html>"


def with_stub(handler, body):
    """Run ``body(egress)`` on a real EgressClient whose socket is ``handler``."""

    async def main():
        client = EgressClient()
        async with client:
            await client._client.aclose()  # noqa: SLF001 - swapping in the transport
            client._client = httpx.AsyncClient(
                transport=httpx.MockTransport(handler),
                headers={"User-Agent": client.settings.user_agent},
            )
            client.limiter = DomainLimiter(1000.0)  # no pacing sleeps in a unit test
            return await body(client)

    return asyncio.run(main())


def responder(robots: httpx.Response | Exception, page: httpx.Response):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            if isinstance(robots, Exception):
                raise robots
            return robots
        return page

    return handler


OK_PAGE = httpx.Response(200, content=PAGE, headers={"content-type": "text/html; charset=utf-8"})


# ---------------------------------------------------------------------------
# FR-183/FR-166: a cache hit is still a stored document
# ---------------------------------------------------------------------------


def test_a_cached_fetch_still_points_at_the_stored_raw_document() -> None:
    """FR-183: the cached body *is* a raw document; the link must not be lost.

    Before this, every fetch served from ``http_cache`` returned
    ``raw_document_id=None``, so provenance rows written on any run inside the
    24 h TTL - the whole point of a second campaign - could not be traced back
    to the page they came from.
    """
    async def twice(egress: EgressClient) -> tuple:
        return (
            await egress.fetch("https://acme.example/careers"),
            await egress.fetch("https://acme.example/careers"),
        )

    first, second = with_stub(responder(httpx.Response(200, text=""), OK_PAGE), twice)

    assert first.from_cache is False and second.from_cache is True
    assert first.raw_document_id, "a live fetch must register a raw document"
    assert second.raw_document_id == first.raw_document_id
    assert second.content_hash == first.content_hash
    # The crawler filters on the content type, so the cached headers matter too.
    assert "html" in second.headers.get("content-type", "")
    stored = query_one("SELECT * FROM raw_document WHERE id = ?", (first.raw_document_id,))
    assert stored and stored["content_hash"] == second.content_hash


def test_a_cache_hit_carries_provenance_into_the_knowledge_base() -> None:
    """FR-166: the row written from a cached page names the document it came from."""
    async def twice(egress: EgressClient):
        await egress.fetch("https://acme.example/jobs")
        return await egress.fetch("https://acme.example/jobs")

    cached = with_stub(responder(httpx.Response(200, text=""), OK_PAGE), twice)
    assert cached.from_cache and cached.raw_document_id, "a cache hit carries its document id"
    writer = KnowledgeBaseWriter(adapter_key="board.stub", plan_item_id="pi-1")
    outcome = writer.write(
        {
            "entity_type": "vacancy",
            "data": {"title": "Data Engineer", "company_name_raw": "Acme NV"},
            "confidence": 0.7,
            "raw_document_id": cached.raw_document_id,
        }
    )
    assert outcome is not None
    provenance = query_all(
        "SELECT * FROM provenance WHERE entity_type = 'vacancy' AND entity_id = ?",
        (outcome.entity_id,),
    )
    assert [p["raw_document_id"] for p in provenance] == [cached.raw_document_id]


# ---------------------------------------------------------------------------
# FR-182: robots.txt that could not be read is unknown, not permissive
# ---------------------------------------------------------------------------


def test_an_unreachable_robots_txt_is_not_remembered_as_permission() -> None:
    """FR-182/CR-402: one failed robots.txt used to disable robots for a day."""
    handler = responder(httpx.ConnectError("no route to host"), OK_PAGE)
    with pytest.raises(RobotsUnavailable):
        with_stub(handler, lambda eg: eg.fetch("https://blocked.example/postings"))

    row = query_one("SELECT * FROM robots_cache WHERE domain = 'blocked.example'")
    assert row is not None
    assert row["body"] is None, "an unread robots.txt must not be stored as 'no rules'"
    # Minutes, not a day: the next run re-asks instead of trusting the failure.
    assert row["expires_at"] > utcnow()
    horizon = ROBOTS_FAILURE_TTL_SECONDS + 120
    from datetime import UTC, datetime, timedelta

    assert row["expires_at"] < (datetime.now(UTC) + timedelta(seconds=horizon)).isoformat()

    # A second, healthy client must not read that row as permission either.
    healthy = responder(httpx.Response(200, text="User-agent: *\nDisallow: /"), OK_PAGE)
    assert with_stub(healthy, lambda eg: eg.allowed("https://blocked.example/postings")) is False


def test_robots_outcomes_are_distinguished() -> None:
    """A site that says no, a site that publishes nothing, and a site we cannot ask."""
    disallow = responder(httpx.Response(200, text="User-agent: *\nDisallow: /"), OK_PAGE)
    absent = responder(httpx.Response(404, text="not found"), OK_PAGE)
    broken = responder(httpx.Response(503, text="down"), OK_PAGE)

    with pytest.raises(RobotsDisallowed) as blocked:
        with_stub(disallow, lambda eg: eg.fetch("https://no.example/jobs"))
    assert not isinstance(blocked.value, RobotsUnavailable)

    served = with_stub(absent, lambda eg: eg.fetch("https://silent.example/jobs"))
    assert served.ok, "a 4xx robots.txt means the site publishes no rules"

    with pytest.raises(RobotsUnavailable):
        with_stub(broken, lambda eg: eg.fetch("https://flaky.example/jobs"))
    assert query_one("SELECT * FROM robots_cache WHERE domain = 'flaky.example'")["body"] is None


def test_using_the_client_outside_its_context_manager_is_a_loud_error() -> None:
    """The exact reproduction: the misuse used to poison robots_cache silently."""
    egress = EgressClient()
    with pytest.raises(RuntimeError, match="context manager"):
        asyncio.run(egress.fetch("https://api.example/v1/postings"))
    row = query_one("SELECT * FROM robots_cache WHERE domain = 'api.example'")
    assert row is None or row["body"] is None


# ---------------------------------------------------------------------------
# The writer: a dropped record is a failure, and it is counted (FR-166)
# ---------------------------------------------------------------------------


def test_a_record_the_writer_cannot_store_is_counted_not_silently_dropped() -> None:
    writer = KnowledgeBaseWriter(adapter_key="board.stub", plan_item_id="pi-1")

    unwrapped = writer.write({"title": "Bare Engineer", "company_name_raw": "Acme"})
    unbindable = writer.write(
        {
            "entity_type": "vacancy",
            "data": {
                "title": "Set Engineer",
                "required_skills": {"python"},  # a set: sqlite3 cannot bind it
            },
        }
    )
    identity_less = writer.write({"entity_type": "vacancy", "data": {"description": "no title"}})

    assert (unwrapped, unbindable, identity_less) == (None, None, None)
    assert writer.total_written == 0
    assert len(writer.failures) == 3
    assert [f.reason for f in writer.failures] == ["unusable_record", "write_error", "rejected"]
    assert writer.summary()["failed"] == 3
    assert writer.last_failure and "vacancy" in writer.last_failure
    assert query_all("SELECT id FROM vacancy") == []


# ---------------------------------------------------------------------------
# FR-184 on a captured real board: 39 openings stay 39 rows
# ---------------------------------------------------------------------------


def collibra_records() -> list:
    raw = RawRecord(
        url="https://boards-api.greenhouse.io/v1/boards/collibra/jobs?content=true",
        content=(FIXTURES / "greenhouse_collibra_jobs.json").read_text(encoding="utf-8"),
        content_type="application/json",
        raw_document_id=None,
        meta={
            "slug": "collibra", "company_id": None, "company_name": "Collibra",
            "max_records": 500, "keywords": [], "title_filter": False,
        },
    )
    adapter = GreenhouseAdapter()
    return [r for r in (adapter.normalise(p, raw) for p in adapter.parse(raw)) if r]


def test_a_real_board_keeps_every_opening_it_advertises() -> None:
    """FR-184: 39 live Collibra postings used to collapse into 28 rows.

    Four distinct openings disappeared outright and eleven were merged onto a
    row that then advertised a different job in a different city, because a
    subset title scored a perfect match and title+employer+date alone reached
    the threshold before the location was even looked at.
    """
    records = collibra_records()
    assert len(records) == 39

    writer = KnowledgeBaseWriter(adapter_key="ats.greenhouse", plan_item_id="pi-1")
    outcomes = writer.write_many(records)

    assert writer.failures == []
    assert sum(1 for o in outcomes if o.created and o.entity_type == "vacancy") == 39
    rows = query_all("SELECT * FROM vacancy")
    assert len(rows) == 39
    titles = sorted(r["title"] for r in rows)
    assert titles == sorted(r.data["title"] for r in records)

    # The pairs that used to be merged, each with its own row and city.
    security = sorted(
        r["location"] for r in rows if r["title"] == "Senior Product Security Engineer"
    )
    assert security == ["Raleigh, North Carolina, USA", "Remote, Europe", "Remote, USA"]


def test_collecting_the_same_board_twice_still_de_duplicates() -> None:
    """FR-184 the other way round: the fix must not turn re-collection into duplication."""
    records = collibra_records()
    KnowledgeBaseWriter(adapter_key="ats.greenhouse", plan_item_id="pi-1").write_many(records)
    second = KnowledgeBaseWriter(adapter_key="ats.greenhouse", plan_item_id="pi-2")
    outcomes = second.write_many(records)

    assert [o.created for o in outcomes] == [False] * 39
    assert len(query_all("SELECT id FROM vacancy")) == 39


def test_a_posting_creates_the_company_it_names() -> None:
    """The ATS chicken-and-egg: a board run must leave companies behind.

    ``financial_year``, ``hiring_signal`` and ``competitor_link`` all declare
    ``company_id NOT NULL``, and ``company.ats_vendor``/``ats_slug`` are what a
    later campaign reads to plan an ATS source.  While the writer left
    ``company_id`` NULL, none of that could ever be populated by the adapters
    that actually retrieve.
    """
    writer = KnowledgeBaseWriter(adapter_key="ats.greenhouse", plan_item_id="pi-1")
    writer.write_many(collibra_records())

    companies = query_all("SELECT * FROM company")
    assert [c["name"] for c in companies] == ["Collibra"]
    company = companies[0]
    assert company["source"] == "ats.greenhouse"
    assert company["country"] is None, "a job's country is not the company's jurisdiction"
    assert float(company["confidence"]) == pytest.approx(0.5)
    assert all(r["company_id"] == company["id"] for r in query_all("SELECT * FROM vacancy"))
    assert query_all(
        "SELECT * FROM provenance WHERE entity_type = 'company' AND entity_id = ?",
        (company["id"],),
    )


# ---------------------------------------------------------------------------
# DR-101 / FR-184: the board identity merges, it never drops a company
# ---------------------------------------------------------------------------


def test_a_second_record_for_a_known_board_merges_instead_of_dropping() -> None:
    """Reproduces the live drop: an existing (vendor, slug) is a merge, not an error.

    The company arrives through the board path with a name spelling the earlier
    row does not carry and no other identity the lookup asks for; before this,
    ``insert_company`` hit ``uq_company_ats_board`` and the writer counted a
    ``write_error`` while the incoming fields were lost.
    """
    writer = KnowledgeBaseWriter(adapter_key="ats.greenhouse", plan_item_id="pi-1")
    first = writer.write(
        {
            "entity_type": "company",
            "confidence": 0.6,
            "data": {"name": "Collibra", "ats_vendor": "greenhouse", "ats_slug": "collibra"},
        }
    )
    assert first is not None and first.created
    before = query_one("SELECT * FROM company WHERE id = ?", (first.entity_id,))

    second = writer.write(
        {
            "entity_type": "company",
            "confidence": 0.7,
            "data": {
                "name": "Collibra NV",
                "domain": "collibra.com",
                "ats_vendor": "greenhouse",
                "ats_slug": "collibra",
            },
        }
    )

    assert second is not None, "a known board identity must not be dropped"
    assert second.created is False
    assert second.entity_id == first.entity_id
    assert writer.failures == [], "the conflict is a merge, not a write_error"
    rows = query_all("SELECT * FROM company")
    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == "Collibra NV", "the incoming non-empty name is merged in"
    assert row["domain"] == "collibra.com"
    assert (row["ats_vendor"], row["ats_slug"]) == ("greenhouse", "collibra")
    assert row["collected_at"] == before["collected_at"], "the first sighting is preserved"
    assert row["refreshed_at"] >= before["collected_at"]


def test_a_conflict_after_a_lookup_miss_still_merges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lookup and the insert are not one transaction; the loser must merge."""
    from dreamjob.pipeline import knowledge_base

    writer = KnowledgeBaseWriter(adapter_key="ats.greenhouse", plan_item_id="pi-1")
    first = writer.write(
        {
            "entity_type": "company",
            "confidence": 0.6,
            "data": {"name": "Collibra", "ats_vendor": "greenhouse", "ats_slug": "collibra"},
        }
    )
    assert first is not None and first.created
    # A race (or a code path that did not ask the board question) makes the
    # pre-insert lookup miss; the unique index is still the authority.
    monkeypatch.setattr(knowledge_base, "resolve_company", lambda data: (None, None))

    second = writer.write(
        {
            "entity_type": "company",
            "confidence": 0.7,
            "data": {
                "name": "Collibra BVBA",
                "domain": "collibra.com",
                "ats_vendor": "greenhouse",
                "ats_slug": "collibra",
            },
        }
    )

    assert second is not None and second.created is False
    assert second.entity_id == first.entity_id
    assert writer.failures == []
    assert len(query_all("SELECT * FROM company")) == 1
    row = query_one("SELECT * FROM company WHERE id = ?", (first.entity_id,))
    assert row["name"] == "Collibra BVBA"
    assert row["domain"] == "collibra.com"


def test_a_legal_identifier_conflict_also_merges(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same guard covers the other unique key on company(FR-184)."""
    from dreamjob.pipeline import knowledge_base

    writer = KnowledgeBaseWriter(adapter_key="kbo", plan_item_id="pi-1")
    first = writer.write(
        {
            "entity_type": "company",
            "confidence": 0.6,
            "data": {"name": "Acme BV", "legal_id": "0123456789", "legal_id_type": "kbo"},
        }
    )
    assert first is not None and first.created
    monkeypatch.setattr(knowledge_base, "resolve_company", lambda data: (None, None))

    second = writer.write(
        {
            "entity_type": "company",
            "confidence": 0.7,
            "data": {
                "name": "Acme Group BV",
                "legal_id": "0123456789",
                "legal_id_type": "kbo",
            },
        }
    )

    assert second is not None and second.created is False
    assert writer.failures == []
    rows = query_all("SELECT * FROM company")
    assert len(rows) == 1
    assert rows[0]["name"] == "Acme Group BV"


def test_a_new_board_identity_still_inserts_a_new_company() -> None:
    """The merge path must not turn every company write into an update."""
    writer = KnowledgeBaseWriter(adapter_key="ats.greenhouse", plan_item_id="pi-1")
    first = writer.write(
        {
            "entity_type": "company",
            "confidence": 0.6,
            "data": {"name": "Alpha BV", "ats_vendor": "greenhouse", "ats_slug": "alpha"},
        }
    )
    second = writer.write(
        {
            "entity_type": "company",
            "confidence": 0.6,
            "data": {"name": "Beta BV", "ats_vendor": "greenhouse", "ats_slug": "beta"},
        }
    )
    assert first is not None and first.created
    assert second is not None and second.created
    assert writer.failures == []
    assert len(query_all("SELECT * FROM company")) == 2


def test_a_merge_never_relabels_the_posting_it_merged_into() -> None:
    """FR-184: a merge enriches a row; it must not repoint it at another job."""
    writer = KnowledgeBaseWriter(adapter_key="board.stub", plan_item_id="pi-1")
    first = writer.write(
        {
            "entity_type": "vacancy",
            "data": {
                "title": "Data Engineer",
                "company_name_raw": "Acme NV",
                "location": "Gent",
                "posted_at": "2026-09-01",
            },
        }
    )
    assert first is not None
    second = writer.write(
        {
            "entity_type": "vacancy",
            "data": {
                "title": "Data Engineer - Gent",
                "company_name_raw": "Acme NV",
                "location": "Gent",
                "posted_at": "2026-09-08",
                "description": "Now with an advert.",
            },
        }
    )
    assert second is not None and second.created is False
    row = query_one("SELECT * FROM vacancy WHERE id = ?", (first.entity_id,))
    assert row["title"] == "Data Engineer", "the surviving row keeps its own title"
    assert row["description"] == "Now with an advert.", "but it is still enriched"


# ---------------------------------------------------------------------------
# The similarity measure itself
# ---------------------------------------------------------------------------


def test_a_subset_title_is_not_a_perfect_match() -> None:
    assert dedup.token_set_ratio("Product Manager", "Product Marketing Manager") < 1.0
    # Noise words are not identity, so these are still the same posting.
    assert dedup.token_set_ratio("Data Engineer (m/v) - Gent", "Gent | Data engineer") == 1.0


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Senior Enterprise Account Executive II", "Senior Account Executive II, Financial Services"),
        ("Senior Sales Compensation Analyst", "Senior People Solutions Analyst"),
        ("Senior Technical Product Marketing Manager", "Senior Partner Product Marketing Manager"),
        ("Senior AI Engineer, Unstructured AI", "Senior AI Engineer"),
        ("AI Engineer", "ML Engineer"),
        ("Software Engineer, Backend", "Software Engineer, Frontend"),
        ("Customer Success Manager", "Customer Support Manager"),
        ("Backend Engineer", "Backend Developer"),
        # From the live Ashby board: the discriminator sits inside the bracket
        # that normalise_title throws away.
        (
            "Senior Strategic Implementation Specialist - Americas (EST)",
            "Senior Strategic Implementation Specialist - Americas (PST)",
        ),
        ("Enterprise Account Executive - Americas (West)", "Enterprise Account Executive (East)"),
    ],
)
def test_two_different_openings_are_never_one_record(left: str, right: str) -> None:
    """Every one of these pairs was merged on a live board (Collibra, Ashby)."""
    base = {"company_name_raw": "Collibra", "location": "Raleigh", "posted_at": "2026-09-01"}
    assert not dedup.is_duplicate_vacancy(dict(base, title=left), dict(base, title=right))


def test_a_bracketed_noise_tag_is_still_noise() -> None:
    """The bracket rule must not stop "(m/v)" and "(80%)" from being ignored."""
    base = {"company_name_raw": "Acme NV", "location": "Gent", "posted_at": "2026-09-01"}
    assert dedup.is_duplicate_vacancy(
        dict(base, title="Data Engineer (m/v)"), dict(base, title="Data Engineer")
    )
    assert dedup.is_duplicate_vacancy(
        dict(base, title="Data Engineer (80%)"), dict(base, title="Data Engineer")
    )


def test_the_same_title_in_two_places_is_two_openings() -> None:
    base = {"title": "Senior Product Security Engineer", "company_name_raw": "Collibra",
            "posted_at": "2026-08-06"}
    assert not dedup.is_duplicate_vacancy(
        dict(base, location="Remote, USA"), dict(base, location="Remote, Europe")
    )
    # ... while one side merely naming the country is the same opening.
    assert dedup.is_duplicate_vacancy(
        dict(base, location="Gent, Belgium"), dict(base, location="9000 Gent")
    )
