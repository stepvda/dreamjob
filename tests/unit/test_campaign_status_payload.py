"""The status payload stays small on a plan of tens of thousands of items.

``source_plan_item`` held 84,041 rows on the installed database, and embedding
one row per item in ``GET /campaigns/{id}/status`` made a ~7 MB response that the
dashboard asked for every four seconds: 6,749 calls delivered ~46 GB, 2,580 of
them slower than a second.  The fix is not to hide the plan - the dashboard
still needs its progress, its per-source table and its outcome ledger - but to
send one bounded page of rows plus exact aggregates, with the full list behind
a paginated endpoint and conditional requests.

These tests pin the contract that makes that safe:

* a plan of 200 items returns only a page of ``sources``, and no row carries the
  raw query, ``caps`` blob or rationale the old payload repeated per item;
* the counters (``sources_total``, ``outcome_states``, the outcome ledger and
  the per-adapter groups) are computed over *every* item, not the page;
* ``?sources_limit``/``sources_offset`` and ``/sources`` reach the rest;
* the response is conditional - a matching ``If-None-Match`` or ``?since=``
  says nothing changed without re-sending the page.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dreamjob.adapters.base import AdapterCapabilities
from dreamjob.api.deps import CurrentSeeker, current_seeker
from dreamjob.config import get_settings
from dreamjob.db.connection import insert_row, upsert_row, utcnow
from dreamjob.db.migrator import migrate
from dreamjob.db.repositories import campaigns as campaign_repo
from dreamjob.pipeline import collection

ADAPTER = "payload.board"
#: Big enough that, repeated 200 times, the fields the old payload embedded
#: would be megabytes.  None of it may reach the response.
BLOB = "x" * 5000


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DREAMJOB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DREAMJOB_DB_PATH", str(tmp_path / "data" / "test.db"))
    get_settings.cache_clear()
    get_settings()
    migrate()
    yield str(get_settings().abs_db_path)
    get_settings.cache_clear()


def _seeker(email: str = "payload@example.com") -> str:
    return insert_row(
        "job_seeker",
        {
            "email": email,
            "display_name": "Payload",
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )


def _campaign(seeker_id: str) -> str:
    directive_id = insert_row(
        "directive_set",
        {
            "job_seeker_id": seeker_id,
            "name": "Payload",
            "created_at": utcnow(),
            "job_content": {"target_titles": ["Data Engineer"]},
            "location": {"countries": ["BE"]},
        },
    )
    profile_id = insert_row(
        "profile_version",
        {"job_seeker_id": seeker_id, "version": 1, "sections": {}, "created_at": utcnow()},
    )
    return campaign_repo.create_campaign(
        seeker_id,
        {
            "name": "Payload",
            "directive_set_id": directive_id,
            "profile_version_id": profile_id,
            "caps": {"max_pages": 20, "max_pages_per_source": 2},
        },
    )


def _catalogue(adapter_key: str) -> None:
    upsert_row(
        "source_catalogue",
        {
            "adapter_key": adapter_key,
            "display_name": "Payload Board",
            "source_type": "job_board",
            "coverage_countries": ["BE"],
            "coverage_industries": [],
            "query_capabilities": AdapterCapabilities(pagination=False).__dict__,
            "access_method": "api",
            "rate_limit_rps": 0.5,
            "cost_per_call_eur": 0.0,
            "tos_status": "permitted",
            "enabled": 1,
            "requires_ack": 0,
            "updated_at": utcnow(),
        },
        ["adapter_key"],
    )


def _seed(campaign_id: str, count: int = 200) -> None:
    """``count`` plan items, each carrying payloads the dashboard never reads."""
    # Four equal buckets so every counter has a known, non-trivial answer.
    buckets = ["succeeded"] * (count // 4) + ["blocked"] * (count // 4) + [
        "gone"
    ] * (count // 4) + ["failed"] * (count // 4)
    status_by_state = {"succeeded": "done", "blocked": "blocked", "gone": "gone",
                       "failed": "failed"}
    for index, state in enumerate(buckets):
        campaign_repo.insert_plan_item(
            campaign_id,
            {
                "adapter_key": ADAPTER,
                "native_query": {"q": f"term-{index}", "payload": BLOB},
                "rationale": BLOB,
                "caps": {
                    "stage": 1,
                    "records_per_page": 10,
                    "payload": BLOB,
                    "outcome": {"state": state, "requests": 1, "refused": 0},
                },
                "estimated_pages": 2,
                "estimated_seconds": 3,
                "status": status_by_state[state],
                "outcome_state": state,
                "outcome_reason": f"reason-{index}: {BLOB}",
                "records_collected": 3 if state == "succeeded" else 0,
                "error_count": 1 if state == "failed" else 0,
                "last_error": f"error-{index}: {BLOB}",
                "activity_at": utcnow(),
                "created_at": utcnow(),
            },
        )


@pytest.fixture()
def seeded(db):
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue(ADAPTER)
    _seed(campaign_id)
    return seeker, campaign_id


@pytest.fixture()
def client(seeded):
    seeker, _campaign_id = seeded
    from dreamjob.api.routers import campaigns as router_module

    app = FastAPI()
    app.include_router(router_module.router, prefix="/api/campaigns")
    app.dependency_overrides[current_seeker] = lambda: CurrentSeeker(
        id=seeker, email="payload@example.test", display_name="Payload", is_admin=False, locale="nl"
    )
    return TestClient(app)


# ---------------------------------------------------------------------------
# The payload is bounded and carries no raw per-item blobs
# ---------------------------------------------------------------------------

RAW_FIELDS = ("native_query", "caps", "rationale")


def test_status_sends_a_bounded_page_and_no_raw_payload(seeded):
    seeker, campaign_id = seeded
    status = collection.status(campaign_id, seeker)

    assert len(status["sources"]) <= collection.DEFAULT_SOURCES_LIMIT
    assert status["sources_total"] == 200
    assert status["sources_truncated"] is True

    for row in status["sources"]:
        assert not (set(RAW_FIELDS) & set(row)), "a raw plan-item field leaked into the page"
        # Error text is a tooltip, not a stack trace.
        assert row["last_error"] is None or len(row["last_error"]) <= collection.SOURCE_TEXT_LIMIT
        assert (
            row["outcome_reason"] is None
            or len(row["outcome_reason"]) <= collection.SOURCE_TEXT_LIMIT
        )

    # A whole response, with all 200 items behind it, is a fraction of a MB -
    # the old payload repeated 5 KB of query and caps once per item.
    assert len(json.dumps(status, separators=(",", ":"))) < 250_000


def test_counters_are_computed_over_every_item_not_the_page(seeded):
    seeker, campaign_id = seeded
    status = collection.status(campaign_id, seeker)

    assert status["outcome_states"] == {
        "succeeded": 50,
        "blocked": 50,
        "gone": 50,
        "failed": 50,
    }
    counts = status["outcomes"]["counts"]
    assert counts["succeeded"] == 50
    assert counts["blocked"] == 50
    assert counts["gone"] == 50
    assert counts["failed"] == 50
    assert counts["running"] == 0 and counts["pending"] == 0
    assert status["outcomes"]["records"] == 150
    assert status["outcomes"]["producing_sources"] == 50

    assert len(status["source_groups"]) == 1
    group = status["source_groups"][0]
    assert group["adapter_key"] == ADAPTER
    assert group["plan_items"] == 200
    assert group["targets"] == 200
    assert group["records"] == 150
    assert group["counts"] == {"done": 50, "blocked": 50, "gone": 50, "failed": 50}


def test_pagination_reaches_the_rest_of_the_plan(seeded):
    seeker, campaign_id = seeded

    first = collection.status(campaign_id, seeker, sources_limit=10, sources_offset=0)
    second = collection.status(campaign_id, seeker, sources_limit=10, sources_offset=10)

    assert first["sources_returned"] == 10
    assert second["sources_returned"] == 10
    assert first["sources_total"] == second["sources_total"] == 200
    first_ids = {row["plan_item_id"] for row in first["sources"]}
    second_ids = {row["plan_item_id"] for row in second["sources"]}
    assert not (first_ids & second_ids), "pages must not overlap"

    # And the request is clamped: no caller can ask the response back to full.
    huge = collection.status(campaign_id, seeker, sources_limit=10_000_000)
    assert huge["sources_limit"] == collection.MAX_SOURCES_LIMIT
    assert huge["sources_returned"] == 200  # the whole plan, this small


def test_the_sources_endpoint_pages_and_searches(seeded):
    seeker, campaign_id = seeded
    page = collection.source_rows(campaign_id, seeker, limit=25, offset=0)
    assert page["total"] == 200
    assert len(page["items"]) == 25
    assert page["truncated"] is True
    for row in page["items"]:
        assert not (set(RAW_FIELDS) & set(row))

    filtered = collection.source_rows(campaign_id, seeker, query=ADAPTER)
    assert filtered["total"] == 200
    assert {row["adapter_key"] for row in filtered["items"]} == {ADAPTER}
    assert collection.source_rows(campaign_id, seeker, query="nothing-here")["total"] == 0

    by_adapter = collection.source_rows(campaign_id, seeker, adapter_key=ADAPTER, limit=200)
    assert by_adapter["total"] == 200
    assert collection.source_rows(campaign_id, seeker, adapter_key="nope")["total"] == 0


# ---------------------------------------------------------------------------
# Conditional requests and the paginated endpoint, over HTTP
# ---------------------------------------------------------------------------


def test_status_is_conditional_on_rev(client, seeded):
    _seeker, campaign_id = seeded
    path = f"/api/campaigns/{campaign_id}/status"

    first = client.get(path)
    assert first.status_code == 200
    rev = first.json()["rev"]
    etag = first.headers["etag"]
    assert rev and etag == f'"{rev}"'

    unchanged = client.get(path, headers={"If-None-Match": etag})
    assert unchanged.status_code == 304
    assert unchanged.content == b""

    same_rev = client.get(path, params={"since": rev})
    assert same_rev.status_code == 200
    assert same_rev.json() == {"campaign_id": campaign_id, "rev": rev, "unchanged": True}

    # A stale revision still gets the payload, not a false "unchanged".
    stale = client.get(path, params={"since": "not-the-rev"})
    assert stale.status_code == 200
    assert stale.json()["rev"] == rev
    assert "sources" in stale.json()


def test_status_and_sources_are_scoped_to_the_owner(client, seeded):
    _owner, campaign_id = seeded
    from dreamjob.api.deps import current_seeker as dep

    other = _seeker("other@example.com")
    client.app.dependency_overrides[dep] = lambda: CurrentSeeker(
        id=other, email="other@example.test", display_name="Other", is_admin=False, locale="nl"
    )
    assert client.get(f"/api/campaigns/{campaign_id}/status").status_code == 404
    assert client.get(f"/api/campaigns/{campaign_id}/sources").status_code == 404


def test_the_sources_endpoint_returns_the_shape_the_ui_reads(client, seeded):
    _seeker, campaign_id = seeded
    body = client.get(
        f"/api/campaigns/{campaign_id}/sources",
        params={"adapter_key": ADAPTER, "limit": 5},
    ).json()
    assert body["total"] == 200
    assert len(body["items"]) == 5
    assert body["truncated"] is True
    row = body["items"][0]
    assert row["adapter_key"] == ADAPTER
    assert "target_key" in row and "label" in row and "status" in row
