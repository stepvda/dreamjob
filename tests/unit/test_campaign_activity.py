"""What the campaign has been doing, and when (FR-361).

A collection run takes four hours and touches 6,524 sources.  The dashboard
could draw a progress bar and an outcomes ledger and never a chronology, so to
the person watching it the whole run was a number that occasionally moved.  The
feed is the other half: the last thing each source did, newest first, with the
time it happened.

Two decisions are on trial here and both are easy to get wrong in the direction
of a feed that says nothing.

* **When the line is written.**  ``_settle`` writes the verdict columns, but it
  runs *once*, after both waves of the whole plan have finished - measured on a
  live run, 7,080 pages in and four hours old, with 0 of 6,524 rows carrying an
  ``outcome_state``.  A feed built on those columns is silent for the length of
  a run and then prints 6,524 lines at the end of it.  The live moment is the
  end of the page loop, in ``_run_unit``'s ``finally``, and that is what
  ``_stamp_finished`` records.
* **Which lines are not written.**  One measured campaign left 4,664 items in
  "not started: max_pages".  They did nothing, and 4,664 identical lines is not
  a chronology; the closing milestone reports them as one number instead.  So a
  unit that never started is never stamped, and a settle pass that merely agrees
  with the live stamp does not repeat it.

Nothing here touches the network: ``EgressClient.fetch`` is replaced by a canned
answer and the adapters are stubs in the normal registry (NFR-601).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from dreamjob.adapters.base import (
    AdapterCapabilities,
    NormalisedRecord,
    PlanItem,
    RawRecord,
    SourceAdapter,
    SourceType,
    register_adapter,
)
from dreamjob.adapters.vacancy_source import SourceUnavailable
from dreamjob.api.routers import campaigns as campaigns_router
from dreamjob.api.routers.campaigns import (
    _activity_events,
    _event_text,
    _milestone_event,
    _source_event,
    campaign_activity,
)
from dreamjob.config import get_settings
from dreamjob.db.connection import (
    insert_row,
    query_all,
    query_one,
    update_row,
    upsert_row,
    utcnow,
)
from dreamjob.db.migrator import migrate
from dreamjob.db.repositories import campaigns as campaign_repo
from dreamjob.egress.client import EgressClient, FetchResult
from dreamjob.jobs.runner import JobContext, runner
from dreamjob.pipeline import collection


@pytest.fixture()
def db(tmp_path, monkeypatch) -> Iterator[None]:
    monkeypatch.setenv("DREAMJOB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DREAMJOB_DB_PATH", str(tmp_path / "data" / "test.db"))
    get_settings.cache_clear()
    get_settings()
    migrate()
    yield
    get_settings.cache_clear()


def _answer(url: str, status: int = 200) -> FetchResult:
    return FetchResult(
        url=url,
        status_code=status,
        text="{}",
        content=b"{}",
        headers={},
        from_cache=False,
        raw_document_id=None,
        content_hash="",
    )


@pytest.fixture()
def answers(monkeypatch):
    """Make every fetch answer one status."""

    def _install(status: int = 200):
        async def _fetch(self, url, **kwargs):
            return _answer(url, status)

        monkeypatch.setattr(EgressClient, "fetch", _fetch)

    return _install


# ---------------------------------------------------------------------------
# Fixtures: one seeker, one campaign, stub adapters of our own
# ---------------------------------------------------------------------------


def _seeker(email: str = "activity@example.com") -> str:
    return insert_row(
        "job_seeker",
        {"email": email, "display_name": "Activity",
         "created_at": utcnow(), "updated_at": utcnow()},
    )


def _campaign(seeker_id: str, **overrides) -> str:
    directive_id = insert_row(
        "directive_set",
        {"job_seeker_id": seeker_id, "name": "Activity", "created_at": utcnow(),
         "job_content": {"target_titles": ["Data Engineer"]},
         "location": {"countries": ["BE"]}},
    )
    profile_id = insert_row(
        "profile_version",
        {"job_seeker_id": seeker_id, "version": 1, "sections": {}, "created_at": utcnow()},
    )
    return campaign_repo.create_campaign(
        seeker_id,
        {"name": "Activity", "directive_set_id": directive_id, "profile_version_id": profile_id,
         "caps": {"max_pages": 20, "max_pages_per_source": 2}, **overrides},
    )


def _catalogue(adapter_key: str, **overrides) -> None:
    row = {
        "adapter_key": adapter_key,
        "display_name": adapter_key,
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
    }
    row.update(overrides)
    upsert_row("source_catalogue", row, ["adapter_key"])


def _plan_item(campaign_id: str, adapter_key: str, query: dict, pages: int = 1) -> str:
    return campaign_repo.insert_plan_item(
        campaign_id,
        {"adapter_key": adapter_key, "native_query": query, "estimated_pages": pages,
         "created_at": utcnow()},
    )


def _run(campaign_id: str, seeker: str) -> str:
    job_id = runner.create(collection.JOB_KIND, campaign_id=campaign_id, job_seeker_id=seeker)
    ctx = JobContext(job_id=job_id, campaign_id=campaign_id, job_seeker_id=seeker, checkpoint={})
    asyncio.run(collection.collection_worker(ctx))
    return job_id


def _rows(campaign_id: str) -> dict[str, dict]:
    return {i["adapter_key"]: i for i in campaign_repo.list_plan_items(campaign_id)}


#: What each plan item's row said about itself while its own ``fetch`` was
#: running.  A stamp that only appears once the run is over is exactly the bug
#: this feature exists to fix, so the assertion is made from inside the run.
SEEN_MID_RUN: dict[str, dict] = {}


class _Base(SourceAdapter):
    capabilities = AdapterCapabilities(pagination=False)
    URL = "https://activity.test/jobs"

    def plan(self, directives, composite_profile, caps):
        return []

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        # ``PlanItem`` carries no row id, and this fixture plans one item per
        # adapter, so the adapter key finds its own row.
        row = query_one(
            "SELECT status, activity_at, activity_kind FROM source_plan_item "
            "WHERE adapter_key = ?",
            (self.key,),
        )
        SEEN_MID_RUN[self.key] = dict(row or {})
        result = await self.egress.fetch(self.URL)
        if not result.ok:
            raise SourceUnavailable(
                f"[{self.key}] 1 request(s), 0 answered; HTTP {result.status_code}"
            )
        return [RawRecord(url=self.URL, content="{}",
                          meta={"title": "Data Engineer", "company_name_raw": "Activity NV",
                                "country": "BE"})]

    def parse(self, raw: RawRecord) -> list[dict]:
        return [dict(raw.meta)]

    def normalise(self, parsed: dict, raw: RawRecord) -> NormalisedRecord | None:
        return NormalisedRecord(entity_type="vacancy", data=dict(parsed))


@register_adapter
class _ActivityBoard(_Base):
    key = "activity.board"
    display_name = "Activity Board (a public employment service)"
    source_type = SourceType.JOB_BOARD


@register_adapter
class _ActivityRegistry(_Base):
    key = "activity.registry"
    display_name = "Activity Registry"
    source_type = SourceType.REGISTRY


# ---------------------------------------------------------------------------
# The live stamp (FR-361)
# ---------------------------------------------------------------------------


def test_a_source_says_it_has_started_while_it_is_still_running(db, answers) -> None:
    """The first line of the feed is written before the source has an answer.

    The row is read from inside the adapter's own ``fetch``, which is the only
    moment that can tell a live stamp from one written on the way out.
    """
    answers()
    SEEN_MID_RUN.clear()
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("activity.board")
    _plan_item(campaign_id, "activity.board", {"queries": ["data"], "locations": ["Brussels"]})

    _run(campaign_id, seeker)

    seen = SEEN_MID_RUN["activity.board"]
    assert seen["activity_kind"] == "started"
    assert seen["activity_at"], "a kind with no time on it is not a chronology"
    assert seen["status"] == "running"


def test_a_source_is_stamped_when_its_page_loop_ends_not_when_the_run_settles(
    db, answers
) -> None:
    """FR-361: the verdict is four hours late, so the feed does not wait for it.

    ``_stamp_finished`` runs in ``_run_unit``'s ``finally``; ``_settle`` runs
    once for the whole plan afterwards.  Both end up agreeing here, which is the
    point: the agreement is what lets the settle pass stay quiet.
    """
    answers()
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("activity.board")
    _plan_item(campaign_id, "activity.board", {"queries": ["data"]})

    _run(campaign_id, seeker)

    row = _rows(campaign_id)["activity.board"]
    assert row["activity_kind"] == "succeeded"
    assert row["activity_kind"] == row["outcome_state"]
    assert row["activity_at"]


def _unit(plan_item_id: str, campaign_id: str, *, pages: int, done: int) -> collection._Unit:
    item = campaign_repo.get_plan_item(plan_item_id, campaign_id)
    assert item is not None
    return collection._Unit(item=item, adapter=None, writer=None, pages=pages, done=done)


def test_a_source_the_page_budget_interrupted_is_not_reported_as_finished(db) -> None:
    """A wave that ends with pages left is the run's decision, not the source's.

    Stamping it would put "succeeded" against a source that has three pages
    still to fetch, and the second wave would then contradict it.
    """
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("activity.board")
    item_id = _plan_item(campaign_id, "activity.board", {"queries": ["data"]}, pages=4)

    unit = _unit(item_id, campaign_id, pages=4, done=1)
    unit.outcome.created = 3
    collection._stamp_finished(unit)

    row = campaign_repo.get_plan_item(item_id, campaign_id)
    assert row["activity_at"] is None and row["activity_kind"] is None

    unit.done = 4
    collection._stamp_finished(unit)
    row = campaign_repo.get_plan_item(item_id, campaign_id)
    assert row["activity_kind"] == "succeeded"
    assert row["activity_at"]
    # Nothing about the verdict is written early: the FR-185 ledger reads
    # ``status`` and ``outcome_state`` first, and must not see this at all.
    assert row["outcome_state"] is None
    assert row["status"] == "planned"


# ---------------------------------------------------------------------------
# What the settle pass adds, and what it must not repeat (FR-361)
# ---------------------------------------------------------------------------


def _settled(unit: collection._Unit, campaign_id: str) -> dict:
    collection._settle(unit, collection.CollectionStats(), campaign_id)
    row = campaign_repo.get_plan_item(unit.id, campaign_id)
    assert row is not None
    return row


def test_the_settle_pass_does_not_repeat_a_line_the_reader_has_already_read(db) -> None:
    """6,524 sources settle at the end of a run; most say what they already said."""
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("activity.board")
    item_id = _plan_item(campaign_id, "activity.board", {"queries": ["data"]})

    unit = _unit(item_id, campaign_id, pages=1, done=1)
    unit.started = True
    unit.outcome.created = 8
    unit.outcome.pages = 1
    collection._stamp_finished(unit)
    stamped = campaign_repo.get_plan_item(item_id, campaign_id)["activity_at"]

    row = _settled(unit, campaign_id)
    assert row["outcome_state"] == "succeeded", "the verdict is still written"
    assert row["activity_at"] == stamped, "and it is not a second line saying the same thing"
    assert row["activity_kind"] == "succeeded"


def test_the_settle_pass_writes_a_line_when_it_changes_the_answer(db) -> None:
    """A correction is news; the alternative is a feed that disagrees in silence."""
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("activity.board")
    item_id = _plan_item(campaign_id, "activity.board", {"queries": ["data"]})

    unit = _unit(item_id, campaign_id, pages=1, done=1)
    unit.started = True
    unit.activity_kind = "succeeded"       # what the live stamp said
    unit.outcome.gone = 1                  # ... and what the settle pass finds
    unit.outcome.gone_reason = "HTTP 404"
    unit.outcome.pages = 1

    row = _settled(unit, campaign_id)
    assert row["outcome_state"] == "gone"
    assert row["activity_kind"] == "gone"
    assert row["activity_at"]


def test_a_source_that_never_started_is_never_a_line_in_the_feed(db, answers) -> None:
    """4,664 items of one campaign ended here.  They did nothing; they say nothing.

    The run below is stopped by its page budget after the first source, leaving
    39 items settled as ``capped`` without ever having issued a request.  The
    closing milestone reports them as one number, which is what a reader of a
    six-line panel can actually use.
    """
    answers()
    seeker = _seeker()
    campaign_id = _campaign(seeker, caps={"max_pages": 1, "max_pages_per_source": 1})
    _catalogue("activity.board")
    for _ in range(40):
        _plan_item(campaign_id, "activity.board", {"queries": ["data"]})

    _run(campaign_id, seeker)

    stamped = query_all(
        "SELECT activity_kind FROM source_plan_item WHERE campaign_id = ? "
        "AND activity_at IS NOT NULL",
        (campaign_id,),
    )
    assert len(stamped) == 1, "one source ran; the other 39 were never started"
    assert stamped[0]["activity_kind"] == "succeeded"
    capped = query_all(
        "SELECT id FROM source_plan_item WHERE campaign_id = ? AND outcome_state = 'capped'",
        (campaign_id,),
    )
    assert len(capped) == 39, "and the budget's decision is still recorded on their rows"


def test_re_running_a_campaign_does_not_replay_the_previous_runs_chronology(
    db, answers
) -> None:
    """NFR-603: a second run's feed must not open with the first run's lines."""
    answers()
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("activity.board")
    _plan_item(campaign_id, "activity.board", {"queries": ["data"]})
    _run(campaign_id, seeker)
    assert _rows(campaign_id)["activity.board"]["activity_at"]

    campaign_repo.reset_plan_progress(campaign_id)

    row = _rows(campaign_id)["activity.board"]
    assert row["activity_at"] is None and row["activity_kind"] is None


# ---------------------------------------------------------------------------
# Reading the feed back (FR-361, CR-408)
# ---------------------------------------------------------------------------


def _stamp(plan_item_id: str, at: str, kind: str) -> None:
    campaign_repo.update_plan_item(plan_item_id, {"activity_at": at, "activity_kind": kind})


def test_the_feed_is_newest_first_and_holds_nothing_that_never_ran(db) -> None:
    """``activity_at >= ''`` excludes NULL on its own, so the index stays pure."""
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("activity.board")
    early = _plan_item(campaign_id, "activity.board", {"queries": ["a"]})
    late = _plan_item(campaign_id, "activity.board", {"queries": ["b"]})
    _plan_item(campaign_id, "activity.board", {"queries": ["c"]})   # never ran
    _stamp(early, "2026-09-10T18:00:00+00:00", "started")
    _stamp(late, "2026-09-10T18:21:38+00:00", "succeeded")

    rows = campaign_repo.list_plan_activity(campaign_id, "", 60)

    assert [r["plan_item_id"] for r in rows] == [late, early]
    assert all(r["at"] for r in rows)


def test_the_cursor_is_inclusive_so_the_boundary_second_is_not_dropped(db) -> None:
    """FR-361: ``utcnow`` writes seconds and a wave settles a dozen inside one.

    An exclusive cursor would drop every event sharing the second the client
    last read, which during the ATS harvest is most of them.  The overlap costs
    a row or two per poll and the client dedupes on the event id.
    """
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("activity.board")
    moment = "2026-09-10T18:21:38+00:00"
    for keyword in ("a", "b", "c"):
        _stamp(_plan_item(campaign_id, "activity.board", {"queries": [keyword]}),
               moment, "succeeded")

    again = campaign_repo.list_plan_activity(campaign_id, moment, 60)

    assert len(again) == 3, "the second the client last read is re-delivered, not skipped"
    assert campaign_repo.list_plan_activity(campaign_id, "2026-09-10T18:21:39+00:00", 60) == []


def test_a_seeker_cannot_read_another_seekers_chronology(db) -> None:
    """FR-101, FR-344: and the answer is 404, not 403.

    A 403 confirms that the campaign exists, which is the fact this endpoint is
    supposed to be keeping.
    """
    from fastapi import HTTPException

    owner = _seeker("owner@example.com")
    other = _seeker("other@example.com")
    campaign_id = _campaign(owner)

    class _Seeker:
        id = other

    with pytest.raises(HTTPException) as raised:
        campaign_activity(campaign_id, _Seeker(), since="", limit=60)
    assert raised.value.status_code == 404


def test_a_limit_nobody_should_ask_for_is_clamped_rather_than_obeyed(db) -> None:
    """200 lines is 33 screenfuls of a six-line panel; 5,000 is a payload."""
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("activity.board")
    for index in range(210):
        _stamp(_plan_item(campaign_id, "activity.board", {"queries": [str(index)]}),
               f"2026-09-10T18:{index % 60:02d}:00+00:00", "succeeded")

    class _Seeker:
        id = seeker

    body = campaign_activity(campaign_id, _Seeker(), since="", limit=5000)
    assert len(body["events"]) == 200
    assert body["truncated"] is True
    assert body["cursor"] == body["server_time"]


def _client(seeker_id: str):
    from datetime import UTC, datetime, timedelta

    from dreamjob.api.routers import campaigns as router_module
    from dreamjob.security.crypto import hash_token
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    insert_row(
        "session",
        {"job_seeker_id": seeker_id, "token_hash": hash_token("activity-token"),
         "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(timespec="seconds"),
         "created_at": utcnow()},
    )
    app = FastAPI()
    app.include_router(router_module.router, prefix="/api/campaigns")
    client = TestClient(app)
    client.headers.update({"Authorization": "Bearer activity-token"})
    return client


def test_the_endpoint_answers_the_shape_the_dashboard_polls_for(db) -> None:
    """FR-361: the contract the panel is written against, over HTTP."""
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("activity.board")
    _stamp(_plan_item(campaign_id, "activity.board", {"queries": ["data"]}),
           "2026-09-10T18:21:38+00:00", "succeeded")
    client = _client(seeker)

    body = client.get(f"/api/campaigns/{campaign_id}/activity").json()

    assert set(body) == {"campaign_id", "server_time", "cursor", "truncated", "events"}
    assert body["truncated"] is False
    event = body["events"][0]
    assert set(event) == {"id", "at", "kind", "source", "text", "detail", "adapter_key",
                          "plan_item_id"}
    assert event["kind"] == "succeeded"
    # The cursor goes straight back as the next ``since``, inclusively.
    again = client.get(f"/api/campaigns/{campaign_id}/activity",
                       params={"since": "2026-09-10T18:21:38+00:00"}).json()
    assert [e["id"] for e in again["events"]] == [event["id"]]
    # ... and a limit outside the contract is refused rather than served.
    assert client.get(f"/api/campaigns/{campaign_id}/activity",
                      params={"limit": 500}).status_code == 422


def test_the_endpoint_does_not_confirm_that_another_seekers_campaign_exists(db) -> None:
    """FR-101, FR-344: 404, because a 403 is itself the answer to a guess."""
    owner = _seeker("api-owner@example.com")
    campaign_id = _campaign(owner)
    intruder = _seeker("api-intruder@example.com")

    assert _client(intruder).get(
        f"/api/campaigns/{campaign_id}/activity"
    ).status_code == 404


# ---------------------------------------------------------------------------
# The words each line carries (FR-361)
# ---------------------------------------------------------------------------


def _row(**overrides) -> dict:
    row = {
        "plan_item_id": "item",
        "at": "2026-09-10T18:21:38+00:00",
        "kind": "succeeded",
        "adapter_key": "ats.ashby",
        "native_query": {"slug": "9-mothers"},
        "outcome_reason": None,
        "last_error": None,
        "records_collected": 8,
        "error_count": 0,
        "display_name": "Ashby",
        "source_type": "ats",
    }
    row.update(overrides)
    return row


#: Every end state ``ItemOutcome.state`` can produce, with the evidence the
#: collection worker writes beside it.
END_STATES = [
    ("succeeded", None, "8 vacancies"),
    ("no_matches", None, "read, nothing to collect"),
    ("blocked", "declined on principle: robots.txt disallows https://x/y",
     "declined, robots.txt says no"),
    ("blocked", "SourceUnavailable: [board.jobat] 6 request(s), 0 answered; HTTP 403",
     "the site refused a bot (HTTP 403)"),
    ("gone", "SourceUnavailable: [ats.ashby] 1 request(s), 0 answered; HTTP 404",
     "the target is gone (HTTP 404)"),
    ("failed", "SourceUnavailable: [board.x] 3 request(s), 0 answered; HTTP 503",
     "the server failed (HTTP 503)"),
    ("failed", "RateLimited: too many requests", "rate limited, nothing collected"),
    ("rejected", None, "the knowledge base refused what it produced"),
    ("normalised_nothing", None, "parsed records, none usable"),
    ("extracted_nothing", None, "fetched a page, read nothing from it"),
    ("no_work", None, "nothing to do"),
    ("cancelled", "cancelled while running; resumable", "cancelled, resumable"),
    ("started", None, "started"),
]


@pytest.mark.parametrize(("kind", "evidence", "text"), END_STATES,
                         ids=[f"{k}-{i}" for i, (k, _e, _t) in enumerate(END_STATES)])
def test_every_end_state_becomes_a_clause_and_never_a_stack_trace(
    kind: str, evidence: str | None, text: str
) -> None:
    """FR-361: the verdict is readable; the raw string goes to the tooltip."""
    event = _source_event(_row(kind=kind, outcome_reason=evidence))
    assert event["kind"] == kind
    assert event["text"] == text
    assert len(event["text"]) <= 48
    assert "Traceback" not in event["text"]
    if evidence:
        assert event["detail"] == evidence
    else:
        assert event["detail"] is None


def test_a_succeeded_line_with_nothing_new_is_not_reported_as_zero() -> None:
    """"0 vacancies" reads like a failure; nothing was collected and nothing broke."""
    assert _source_event(_row(records_collected=0))["text"] == "read, nothing new"
    assert _source_event(_row(records_collected=1))["text"] == "1 vacancy"
    assert _source_event(_row(records_collected=1200))["text"] == "1,200 vacancies"


def test_the_noun_follows_the_source_and_not_the_adapter_key() -> None:
    """``records_collected`` mixes entity types, so the count cannot name itself."""
    assert _source_event(_row(source_type="registry"))["text"] == "8 filings"
    assert _source_event(_row(source_type="directory"))["text"] == "8 companies"
    assert _source_event(_row(source_type="website"))["text"] == "8 pages"
    assert _source_event(_row(source_type="news"))["text"] == "8 articles"
    assert _source_event(_row(source_type="compensation"))["text"] == "8 pay benchmarks"
    # A source type this table has never seen still gets a noun.
    assert _source_event(_row(source_type="patents"))["text"] == "8 records"


def test_an_end_state_nobody_taught_this_endpoint_about_is_shown_and_not_dropped() -> None:
    """A feed that quietly omits what it does not recognise is the old bug again."""
    assert _event_text("some_new_state", _row()) == "some new state"


def test_a_line_names_the_source_the_way_the_per_source_list_does() -> None:
    """FR-162: the same two screens, the same name for the same row."""
    event = _source_event(_row(display_name="EURES (European Job Mobility Portal)",
                               adapter_key="board.eures", kind="started",
                               native_query={"nuts_codes": ["BE1"], "nace_section": "A"}))
    assert event["source"] == "EURES · BE1 · NACE A"
    assert event["id"] == "item:2026-09-10T18:21:38+00:00"
    assert event["plan_item_id"] == "item"
    assert event["adapter_key"] == "board.eures"


# ---------------------------------------------------------------------------
# Milestones (FR-361)
# ---------------------------------------------------------------------------


def test_the_campaigns_own_milestones_read_as_sentences() -> None:
    """The numbers are the ones the audit row recorded, not ones invented here."""
    def _text(action: str, detail: dict) -> str:
        event = _milestone_event({"id": "e", "action": action, "detail": detail,
                                  "at": "2026-09-10T14:09:53+00:00"})
        assert event is not None
        assert event["source"] is None and event["plan_item_id"] is None
        return event["text"]

    assert _text("campaign.created", {"name": "Search 10 Sept"}) == "Campaign created"
    assert _text("campaign.plan_generated", {"sources": 4843, "targets": 4576}) == (
        "Plan ready — 4,843 sources over 4,576 targets"
    )
    assert _text("campaign.collection_started",
                 {"plan_items": 2615, "caps": {"max_pages": 10000}}) == (
        "Collection started — 2,615 sources, 10,000-page cap"
    )
    assert _text("campaign.collection_finished", {"records": 137, "pages": 7}) == (
        "Collection finished — 137 records from 7 pages"
    )
    assert _text("campaign.collection_cancelled", {}) == "Collection cancelled"
    assert _text("campaign.stage_rerun", {"stage": "opportunities"}) == (
        "Stage re-run — opportunities"
    )


def test_a_milestone_whose_numbers_were_never_recorded_says_only_what_it_knows() -> None:
    """An older campaign recorded fewer keys.  "0 sources" would be a lie."""
    event = _milestone_event({"id": "e", "action": "campaign.plan_generated", "detail": None,
                              "at": "2026-09-10T14:09:53+00:00"})
    assert event is not None
    assert event["text"] == "Plan ready"


def test_an_action_from_another_namespace_is_not_this_feeds_to_render() -> None:
    """``audit_event`` is the whole product's ledger; this is one campaign's feed."""
    assert _milestone_event({"id": "e", "action": "source.acknowledged", "detail": {},
                             "at": "2026-09-10T14:00:00+00:00"}) is None


def test_the_three_chronologies_merge_newest_first() -> None:
    """FR-361: sorted on ``(at, id)`` so one second's worth cannot reshuffle."""
    events = _activity_events(
        [_row(plan_item_id="a", at="2026-09-10T18:21:38+00:00"),
         _row(plan_item_id="b", at="2026-09-10T18:21:38+00:00", kind="gone")],
        [{"id": "m1", "action": "campaign.collection_started",
          "detail": {"plan_items": 3}, "at": "2026-09-10T14:10:16+00:00"}],
        [{"id": "j1", "kind": "collection", "started_at": "2026-09-10T14:10:17+00:00",
          "finished_at": None, "progress_done": 7}],
    )
    assert [e["at"] for e in events] == sorted((e["at"] for e in events), reverse=True)
    assert [e["id"] for e in events][:2] == [
        "b:2026-09-10T18:21:38+00:00", "a:2026-09-10T18:21:38+00:00"
    ]
    assert events[-1]["kind"] == "collection_started"
    assert events[-2]["text"] == "Collection job started"


def test_a_run_with_nothing_settled_yet_still_has_a_beginning(db, answers) -> None:
    """The first minutes of a run must not look like a failure to start one."""
    answers()
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("activity.board")
    _plan_item(campaign_id, "activity.board", {"queries": ["data"]})
    job_id = _run(campaign_id, seeker)
    # ``runner.run`` stamps these; this fixture calls the worker directly, so it
    # stamps them the same way rather than pretending a job has no beginning.
    update_row("job_run", job_id,
               {"started_at": "2026-09-10T14:10:16+00:00", "progress_done": 1})

    class _Seeker:
        id = seeker

    body = campaign_activity(campaign_id, _Seeker(), since="", limit=60)
    kinds = {event["kind"] for event in body["events"]}
    # ``campaign.collection_started`` is written by ``collection.launch``, which
    # this fixture bypasses; the job row's own moments are what a feed opened
    # thirty seconds into a run actually has.
    assert "job_started" in kinds
    assert "collection_finished" in kinds
    assert "succeeded" in kinds
    assert body["campaign_id"] == campaign_id
    assert body["events"] == sorted(body["events"], key=lambda e: (e["at"], e["id"]),
                                    reverse=True)


def test_the_cursor_is_never_newer_than_the_rows_it_was_read_with(db, monkeypatch) -> None:
    """FR-361: a stamp written while the handler is reading must survive the poll.

    The cursor the client sends back is a *watermark*: everything at or before
    it has been delivered.  Taking that watermark after the three reads makes it
    a claim the reads cannot support - a source stamped in the second the reads
    began, but after they ran, is in neither this response nor the next one,
    because the next one asks for ``>= now`` and the row is older than that.
    The line is then lost for the rest of the run, and ``activity_at`` is a
    single column per item, so nothing ever restates it.

    Taking the watermark first costs the overlap the inclusive cursor was
    already built for - a row or two re-delivered, deduped on the event id.
    """
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("activity.board")
    concurrent = _plan_item(campaign_id, "activity.board", {"queries": ["a"]})

    reads = "2026-09-10T18:21:38+00:00"
    clock = {"at": reads}
    monkeypatch.setattr(campaigns_router, "utcnow", lambda: clock["at"])

    real = campaigns_router.repo.list_plan_activity

    def _read_then_a_stamp_lands(*args, **kwargs):
        rows = real(*args, **kwargs)
        # The worker stamps a source in the second the read began, just after
        # the read ran, and the handler's clock ticks over before it asks.
        _stamp(concurrent, reads, "succeeded")
        clock["at"] = "2026-09-10T18:21:39+00:00"
        return rows

    monkeypatch.setattr(campaigns_router.repo, "list_plan_activity", _read_then_a_stamp_lands)

    class _Seeker:
        id = seeker

    body = campaign_activity(campaign_id, _Seeker(), since="", limit=60)
    assert [e["id"] for e in body["events"]] == [], "the stamp landed after the read"

    monkeypatch.setattr(campaigns_router.repo, "list_plan_activity", real)
    again = campaign_activity(campaign_id, _Seeker(), since=body["cursor"], limit=60)

    assert [e["plan_item_id"] for e in again["events"]] == [concurrent], (
        "the next poll must still carry the stamp the first one raced"
    )
