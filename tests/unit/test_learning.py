"""Response capture and outcome-segment learning.

The scenario under test is the one the product owner described: a lot of
rejections for a certain kind of job, and the question of whether the system
notices and says something useful about it.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from dreamjob.db import connection as conn_mod
from dreamjob.db.connection import insert_row, utcnow
from dreamjob.db.migrator import migrate


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A migrated database of its own, so tests never touch real data."""
    path = tmp_path / "test.db"
    migrate(path)

    real_get = conn_mod.get_connection

    def pinned(db_path: Path | None = None) -> sqlite3.Connection:
        return real_get(path)

    monkeypatch.setattr(conn_mod, "get_connection", pinned)
    for mod in ("dreamjob.db.repositories.learning",):
        __import__(mod)
    yield path


def _seed(days_ago: int = 40) -> str:
    """A job seeker whose data-engineering applications land and whose
    data-science applications do not."""
    seeker_id = insert_row(
        "job_seeker",
        {
            "email": "test@example.com",
            "display_name": "Test Seeker",
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )
    sent = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat(timespec="seconds")

    company_id = insert_row(
        "company",
        {
            "normalised_name": "acme",
            "name": "Acme",
            "size_band": "50-250",
            "stage": "scaleup",
            "collected_at": utcnow(),
        },
    )
    big_id = insert_row(
        "company",
        {
            "normalised_name": "megacorp",
            "name": "MegaCorp",
            "size_band": ">1000",
            "stage": "listed",
            "collected_at": utcnow(),
        },
    )

    def application(family: str, company: str, replied: bool, rejected: bool) -> None:
        opp_id = insert_row(
            "opportunity",
            {
                "job_seeker_id": seeker_id,
                "campaign_id": campaign_id,
                "company_id": company,
                "kind": "vacancy",
                "title": f"{family} role",
                "function_family": family,
                "seniority": "senior",
                "country": "BE",
                "work_arrangement": "hybrid",
                "language": "en",
                "score": 70,
                "created_at": sent,
                "updated_at": sent,
            },
        )
        insert_row(
            "pipeline_card",
            {
                "job_seeker_id": seeker_id,
                "opportunity_id": opp_id,
                "stage": "closed" if rejected else ("replied" if replied else "sent"),
                "outcome": "rejected" if rejected else None,
                "stage_dates": {"sent": sent},
                "variables": {"cv_template": "classic", "language": "en"},
                "created_at": sent,
                "updated_at": sent,
            },
        )

    campaign_ds = insert_row(
        "directive_set",
        {"job_seeker_id": seeker_id, "name": "d", "created_at": utcnow()},
    )
    profile_id = insert_row(
        "profile_version",
        {
            "job_seeker_id": seeker_id,
            "version": 1,
            "sections": {},
            "created_at": utcnow(),
        },
    )
    campaign_id = insert_row(
        "campaign",
        {
            "job_seeker_id": seeker_id,
            "directive_set_id": campaign_ds,
            "profile_version_id": profile_id,
            "name": "c",
            "created_at": utcnow(),
        },
    )

    # Data engineering: 8 applications, 5 replies.
    for i in range(8):
        application("data engineering", company_id, replied=i < 5, rejected=False)
    # Data science: 9 applications, 1 reply, 8 rejections.
    for i in range(9):
        application("data science", big_id, replied=i < 1, rejected=i >= 1)

    return seeker_id


def test_segments_separate_a_weak_kind_of_work(db):
    """The weak segment is found, and reported with its sample size."""
    from dreamjob.postapp.segments import analyse_segments

    seeker_id = _seed()
    analysis = analyse_segments(seeker_id, outcome="reply", store=False)

    assert analysis.resolved_size == 17
    families = {s.value: s for s in analysis.segments["function_family"]}

    assert families["data engineering"].n == 8
    assert families["data engineering"].successes == 5
    assert families["data science"].n == 9
    assert families["data science"].successes == 1

    # The weak one is below baseline and is surfaced as such.
    assert families["data science"].lift < 0
    assert any(s.value == "data science" for s in analysis.weakest)
    assert any(s.value == "data engineering" for s in analysis.strongest)

    # Every readable sentence names its n - that is the whole point.
    for sentence in analysis.as_dict()["readable"]["weakest"]:
        assert " of " in sentence


def test_small_samples_are_labelled_not_hidden(db):
    """Three applications is not a pattern, and the analysis says so."""
    from dreamjob.postapp.segments import analyse_segments

    seeker_id = _seed()
    analysis = analyse_segments(seeker_id, outcome="reply", store=False)

    assert analysis.caveats, "an analysis must always state its limits"
    assert any("not causes" in c or "noise" in c or "trust" in c for c in analysis.caveats)


def test_unresolved_applications_are_not_counted_as_silence(db):
    """An application sent yesterday is not evidence of being ignored."""
    from dreamjob.postapp.segments import analyse_segments

    seeker_id = _seed(days_ago=1)
    analysis = analyse_segments(seeker_id, outcome="reply", store=False)

    # Cards still in 'sent' with no reply and no silence window elapsed are
    # excluded; only the replied and closed ones resolve.
    assert analysis.resolved_size < analysis.sample_size


def test_advice_refuses_to_invent_a_segment(db):
    """A proposal about a segment nobody applied to is dropped."""
    from dreamjob.postapp.redirection import _validate
    from dreamjob.postapp.segments import analyse_segments

    seeker_id = _seed()
    analysis = analyse_segments(seeker_id, outcome="reply", store=False)

    kept = _validate(
        [
            {
                "dimension": "function_family",
                "from_value": "data science",
                "to_value": "data engineering",
                "headline": "Shift towards data engineering",
            },
            {
                "dimension": "function_family",
                "from_value": "quantum alchemy",   # never applied to
                "to_value": "data engineering",
                "headline": "Invented",
            },
            {"dimension": "not_a_dimension", "headline": "Nonsense"},
        ],
        analysis,
    )

    assert len(kept) == 1
    assert kept[0]["from_value"] == "data science"
    # The figures are re-attached from the analysis, not from the model.
    assert kept[0]["evidence"]["from"]["n"] == 9
    assert kept[0]["evidence"]["to"]["n"] == 8
    # And the effect is recomputed rather than trusted.
    assert kept[0]["expected_effect_points"] == pytest.approx(51.4, abs=0.5)


def test_advice_declines_when_there_is_too_little_data(db):
    """Under the threshold it says so rather than guessing."""
    from dreamjob.postapp.redirection import generate_advice

    seeker_id = insert_row(
        "job_seeker",
        {
            "email": "sparse@example.com",
            "display_name": "Sparse",
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )
    result = generate_advice(seeker_id, outcome="reply", store=False)

    assert result["insufficient_data"] is True
    assert result["proposals"] == []
    assert "resolved" in result["summary"]


def test_manual_response_is_recorded_and_classified_as_stated(db):
    """A hand-entered rejection becomes a reply row with the stated outcome."""
    from dreamjob.db.connection import query_one
    from dreamjob.postapp.response_intake import record_response

    seeker_id = _seed()
    opp = query_one(
        "SELECT id FROM opportunity WHERE job_seeker_id = ? LIMIT 1", (seeker_id,)
    )

    result = record_response(
        seeker_id,
        opportunity_id=opp["id"],
        channel="phone",
        raw_text="They called to say they went with someone else.",
        stated_outcome="rejection",
        classify_text=False,          # no network in tests
    )

    assert result["effective_outcome"] == "rejection"
    reply = query_one(
        "SELECT * FROM incoming_reply WHERE id = ?", (result["incoming_reply_id"],)
    )
    assert reply["classification"] == "rejection"
    assert reply["classification_confidence"] == 1.0

    manual = query_one(
        "SELECT * FROM manual_response WHERE id = ?", (result["manual_response_id"],)
    )
    assert manual["channel"] == "phone"
    assert manual["stated_outcome"] == "rejection"


def test_response_requires_something_to_attach_to(db):
    from dreamjob.postapp.response_intake import record_response

    seeker_id = _seed()
    with pytest.raises(ValueError, match="dispatch or an opportunity"):
        record_response(seeker_id, raw_text="orphan", classify_text=False)


# ---------------------------------------------------------------------------
# The intake route (FR-285, NFR-702)
# ---------------------------------------------------------------------------


@pytest.fixture()
def responses_api(db):
    """Only the learning router, with authentication stubbed out."""
    from dreamjob.api.deps import CurrentSeeker, current_seeker
    from dreamjob.api.routers import learning as router_module
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    seeker_id = _seed()
    app = FastAPI()
    app.include_router(router_module.router, prefix="/api/learning")
    app.dependency_overrides[current_seeker] = lambda: CurrentSeeker(
        id=seeker_id, email="seeker@example.com", display_name="S", is_admin=False, locale="en"
    )
    with TestClient(app) as client:
        yield client, seeker_id


def _one_opportunity(seeker_id: str) -> str:
    from dreamjob.db.connection import query_one

    return query_one("SELECT id FROM opportunity WHERE job_seeker_id = ? LIMIT 1", (seeker_id,))[
        "id"
    ]


def test_recording_a_response_answers_201_and_lands_in_the_audit_trail(responses_api):
    """FR-285 plus NFR-702, pinned at the route.

    The intake writes three rows before it audits, so anything that raises
    *after* those writes leaves the caller unable to tell what landed.  The
    route is exercised here rather than ``record_response`` alone because the
    defect this guards against - the audit call and its declared signature
    drifting apart - only exists at the call site.
    """
    from dreamjob.security.audit import trail_for_entity

    client, seeker_id = responses_api
    response = client.post(
        "/api/learning/responses",
        json={
            "opportunity_id": _one_opportunity(seeker_id),
            "channel": "phone",
            "stated_outcome": "rejection",
            "raw_text": "They rang to say no.",
            "classify": False,
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["effective_outcome"] == "rejection"

    trail = trail_for_entity("incoming_reply", body["incoming_reply_id"])
    assert [row["action"] for row in trail] == ["response.recorded"]
    assert trail[0]["job_seeker_id"] == seeker_id


def test_a_failing_audit_trail_does_not_lose_the_response(responses_api, monkeypatch):
    """NFR-702 must never cost the record it is describing.

    The response rows are written before the audit event, so an audit that
    raises would answer 500 for work that is already durably done - and the
    caller would have no way to know.  Auditing is best-effort by design.
    """
    from dreamjob.db.connection import query_one
    from dreamjob.db.repositories import admin as admin_repo

    def broken(**_kwargs):
        raise sqlite3.OperationalError("audit_event is unavailable")

    monkeypatch.setattr(admin_repo, "insert_audit", broken)

    client, seeker_id = responses_api
    response = client.post(
        "/api/learning/responses",
        json={
            "opportunity_id": _one_opportunity(seeker_id),
            "channel": "linkedin",
            "stated_outcome": "interest",
            "classify": False,
        },
    )

    assert response.status_code == 201, response.text
    manual = query_one(
        "SELECT * FROM manual_response WHERE id = ?", (response.json()["manual_response_id"],)
    )
    assert manual["channel"] == "linkedin"
    # The audit really did fail: without this the test would pass on a route
    # that never audits at all.
    assert query_one("SELECT COUNT(*) AS n FROM audit_event")["n"] == 0


def test_ready_learning_is_announced_without_being_applied(db):
    """FR-425: the analysis and the nudge existed; nothing said it was ready.

    The scheduler announces readiness and leaves adopting the defaults to the
    job seeker (NFR-305), so the weight table is untouched by the sweep.
    """
    from dreamjob.db.connection import query_all
    from dreamjob.db.repositories import opportunities as opp_repo
    from dreamjob.monitoring.scheduler import _learning_sweep

    seeker_id = _seed()
    weights_before = opp_repo.get_weights(seeker_id)

    result = _learning_sweep()

    assert result["notified"] == 1
    notifications = query_all(
        "SELECT kind, dedup_key FROM notification WHERE job_seeker_id = ?", (seeker_id,)
    )
    assert any(n["kind"] == "learning_ready" for n in notifications)
    # Announcing is not applying.
    assert opp_repo.get_weights(seeker_id) == weights_before

    # The same sample is not announced twice.
    assert _learning_sweep()["notified"] == 0

