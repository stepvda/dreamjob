"""Autopilot chain: order, checkpointing, and honest failure (FR-161..166, FR-121..128).

The chain is the product's three-step promise, so what these tests pin down is
not any one stage's behaviour — those have their own tests — but that the
stages run in the right order, that a resumed run continues rather than
repeats, and that a collection failure stops the run and says so instead of
carrying on against an empty corpus.

No network and no model: every expensive stage is replaced with a recorder.
"""

from __future__ import annotations

import asyncio

import pytest
from dreamjob.db.connection import from_json, insert_row, query_one, update_row, utcnow
from dreamjob.db.repositories import seekers as seeker_repo
from dreamjob.jobs.runner import JobCancelled, runner


@pytest.fixture()
def seeker() -> str:
    # A unique e-mail per test: the unit suite shares one scratch database for
    # the whole session, so a fixed address collides on the second test.
    from uuid import uuid4

    return seeker_repo.create_seeker(
        f"autopilot-{uuid4().hex[:8]}@example.com",
        "Autopilot Tester",
        password_hash="x",
        is_admin=False,
    )


@pytest.fixture()
def profile(seeker: str) -> str:
    return insert_row(
        "profile_version",
        {
            "job_seeker_id": seeker,
            "version": 1,
            "sections": {"summary": "Engineer"},
            "dream_job_statement": "Hands-on software design",
            "created_at": utcnow(),
        },
    )


@pytest.fixture()
def recorder(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Replace every stage with a recorder that returns a plausible value.

    ``order`` is appended to as each stage runs, which is what lets a test
    assert the sequence rather than merely that each function was reached.
    """
    from dreamjob.pipeline import autopilot

    calls: dict = {"order": [], "notifications": [], "rescored": 0}

    def _stage(name: str, value):
        calls["order"].append(name)
        return value

    monkeypatch.setattr(autopilot, "_build_composite", lambda s, o: _stage("composite", {"id": "comp1", "version": 1}))
    monkeypatch.setattr(autopilot, "_build_dream_job", lambda s, o: _stage("dream_job", {"id": "dream1"}))
    monkeypatch.setattr(
        autopilot,
        "_save_proposed_directives",
        lambda s, o: _stage("directives", ("dir1", "Autopilot directives")),
    )
    monkeypatch.setattr(
        autopilot, "_create_campaign", lambda s, c, o: _stage("campaign", ("camp1", "Autopilot run"))
    )
    monkeypatch.setattr(autopilot, "_plan", lambda c, s, o: _stage("plan", {"sources": 4}))
    monkeypatch.setattr(autopilot, "_profile_companies", _fake_profile(calls))
    monkeypatch.setattr(autopilot, "_rescore", _fake_rescore(calls))
    monkeypatch.setattr(autopilot, "_opportunity_counts", lambda c: {"opportunities": 7, "speculative": 2, "scored": 7})
    monkeypatch.setattr(autopilot, "_finalise_notification", _fake_notify(calls))
    return calls


def _fake_profile(calls: dict):
    async def run(campaign_id, seeker_id, options):
        calls["order"].append("profiling")
        return {"profiles": {"built": 3}, "financials": {"analysed": 3}}

    return run


def _fake_rescore(calls: dict):
    def run(campaign_id, options):
        calls["order"].append("rescore")
        calls["rescored"] += 1
        return {"scored": 7}

    return run


def _fake_notify(calls: dict):
    def run(seeker_id, campaign_id, report, *, ok):
        calls["order"].append("notify" if ok else "notify_failed")
        calls["notifications"].append({"ok": ok, "report": report})

    return run


def _collection_launch(outcome: str, calls: dict):
    async def launch(campaign_id, seeker_id):
        calls["order"].append("collection_start")
        return "colljob1"

    async def wait(ctx, job_id, campaign_id):
        calls["order"].append(f"collection_wait:{outcome}")
        if outcome == "done":
            return {
                "job_id": job_id,
                "outcome": "done",
                "sources": {"records": 1},
            }
        from dreamjob.pipeline import autopilot

        raise autopilot.AutopilotError(
            f"collection job {job_id} ended {outcome}",
            collection={
                "job_id": job_id,
                "outcome": outcome,
                "reason": "collection_failed",
                "sources": {},
                "last_error": "every source was refused",
            },
        )

    return launch, wait


def _make_ctx(seeker_id: str, options: dict | None = None):
    """A real JobContext backed by a real job_run row in the scratch database."""
    from dreamjob.jobs.runner import JobContext, JobControl

    job_id = insert_row(
        "job_run",
        {
            "job_seeker_id": seeker_id,
            "kind": "autopilot",
            "status": "running",
            "progress_total": 8,
            "created_at": utcnow(),
        },
    )
    return JobContext(
        job_id=job_id,
        job_seeker_id=seeker_id,
        checkpoint={"options": options or {}},
        _control=JobControl(),
    )


def _drain(ctx) -> None:
    from dreamjob.pipeline import autopilot

    async def run() -> None:
        async for _ in autopilot.autopilot_worker(ctx):  # type: ignore[union-attr]
            pass

    asyncio.run(run())


def test_the_chain_runs_in_order_and_reaches_the_shortlist(seeker, profile, recorder, monkeypatch):
    from dreamjob.pipeline import autopilot

    launch, wait = _collection_launch("done", recorder)
    monkeypatch.setattr(autopilot, "_start_collection", launch)
    monkeypatch.setattr(autopilot, "_await_collection", wait)

    ctx = _make_ctx(seeker, {"company_limit": 5, "use_llm": False})
    _drain(ctx)

    assert recorder["order"] == [
        "composite",
        "dream_job",
        "directives",
        "campaign",
        "plan",
        "collection_start",
        "collection_wait:done",
        "profiling",
        "rescore",
        "notify",
    ]
    assert recorder["notifications"][-1]["ok"] is True
    assert recorder["notifications"][-1]["report"]["counts"]["opportunities"] == 7


def test_every_stage_is_checkpointed_so_a_resume_does_not_repeat(seeker, profile, recorder, monkeypatch):
    from dreamjob.pipeline import autopilot

    launch, wait = _collection_launch("done", recorder)
    monkeypatch.setattr(autopilot, "_start_collection", launch)
    monkeypatch.setattr(autopilot, "_await_collection", wait)

    ctx = _make_ctx(seeker)
    _drain(ctx)

    checkpoint = ctx.checkpoint
    for key in (
        "composite_id",
        "dream_job_model_id",
        "directive_set_id",
        "campaign_id",
        "planned",
        "collected",
        "profiled",
    ):
        assert key in checkpoint, f"{key} was not checkpointed"


def test_a_resumed_run_skips_the_stages_already_done(seeker, profile, recorder, monkeypatch):
    """A restart continues from the checkpoint instead of rebuilding everything."""
    from dreamjob.pipeline import autopilot

    launch, wait = _collection_launch("done", recorder)
    monkeypatch.setattr(autopilot, "_start_collection", launch)
    monkeypatch.setattr(autopilot, "_await_collection", wait)

    ctx = _make_ctx(seeker)
    # Everything up to and including collection is already done.
    ctx.checkpoint.update(
        {
            "composite_id": "comp1",
            "dream_job_model_id": "dream1",
            "directive_set_id": "dir1",
            "campaign_id": "camp1",
            "planned": True,
            "collected": True,
            "stage_index": 6,
        }
    )
    _drain(ctx)

    # Only the tail ran.
    assert recorder["order"] == ["profiling", "rescore", "notify"]


def test_a_failed_collection_stops_the_run_and_warns(seeker, profile, recorder, monkeypatch):
    from dreamjob.pipeline import autopilot

    launch, wait = _collection_launch("failed", recorder)
    monkeypatch.setattr(autopilot, "_start_collection", launch)
    monkeypatch.setattr(autopilot, "_await_collection", wait)

    ctx = _make_ctx(seeker)
    with pytest.raises(autopilot.AutopilotError) as failure:
        _drain(ctx)

    assert failure.value.collection["last_error"] == "every source was refused"
    # Profiling never ran: there was nothing to profile against.
    assert "profiling" not in recorder["order"]
    assert recorder["order"][-1] == "notify_failed"
    assert recorder["notifications"][-1]["ok"] is False
    assert recorder["notifications"][-1]["report"]["reason"] == "collection_failed"
    # The run stopped at collection, and the checkpoint says why (never "done").
    assert ctx.checkpoint["collected"] == "failed"


def test_a_missing_dream_job_statement_is_a_skip_not_a_failure(seeker, profile, monkeypatch):
    """FR-128 is optional: the run continues on the profile alone."""
    from dreamjob.pipeline import autopilot
    from dreamjob.pipeline.dreamjob_model import StatementMissing

    def no_statement(_seeker_id: str, _options):
        raise StatementMissing("nothing written")

    monkeypatch.setattr(autopilot, "_build_dream_job", no_statement)
    # require_dream_job False -> swallowed and reported as skipped.
    assert autopilot._build_dream_job.__name__ == "no_statement"

    # Call the real helper with require=False to prove the swallow path.
    monkeypatch.undo()
    monkeypatch.setattr(
        "dreamjob.pipeline.dreamjob_model.build_dream_job_model",
        lambda *a, **k: (_ for _ in ()).throw(StatementMissing("x")),
    )
    assert autopilot._build_dream_job(seeker, autopilot.AutopilotOptions()) is None
    with pytest.raises(StatementMissing):
        autopilot._build_dream_job(seeker, autopilot.AutopilotOptions(require_dream_job=True))


def test_options_round_trip_through_a_checkpoint(seeker):
    """A resumed job reads the same options the user chose."""
    from dreamjob.pipeline.autopilot import AutopilotOptions

    chosen = AutopilotOptions(company_limit=3, use_llm=False, max_pages=40)
    restored = AutopilotOptions.from_checkpoint({"options": chosen.to_dict()})
    assert restored == chosen
    # Unknown keys in a checkpoint are ignored rather than raising.
    assert AutopilotOptions.from_checkpoint({"options": {"nonsense": 1}}).company_limit == 12


# ---------------------------------------------------------------------------
# The collection child's outcome decides the run's outcome (D1-D4)
# ---------------------------------------------------------------------------


def _campaign_row(seeker_id: str, *, status: str = "completed") -> str:
    directive_id = insert_row(
        "directive_set",
        {"job_seeker_id": seeker_id, "name": "Autopilot test", "created_at": utcnow()},
    )
    profile = query_one(
        "SELECT id FROM profile_version WHERE job_seeker_id = ? ORDER BY version DESC LIMIT 1",
        (seeker_id,),
    )
    profile_id = profile["id"] if profile else insert_row(
        "profile_version",
        {"job_seeker_id": seeker_id, "version": 1, "sections": {}, "created_at": utcnow()},
    )
    return insert_row(
        "campaign",
        {
            "job_seeker_id": seeker_id,
            "directive_set_id": directive_id,
            "profile_version_id": profile_id,
            "name": "Autopilot test",
            "status": status,
            "created_at": utcnow(),
        },
    )


def _collection_child(
    seeker_id: str,
    campaign_id: str,
    *,
    status: str = "done",
    stats: dict | None = None,
    last_error: str | None = None,
    error_count: int = 0,
) -> str:
    return insert_row(
        "job_run",
        {
            "job_seeker_id": seeker_id,
            "campaign_id": campaign_id,
            "kind": "collection",
            "status": status,
            "error_count": error_count,
            "last_error": last_error,
            "checkpoint": {"stats": stats or {}},
            "created_at": utcnow(),
        },
    )


@pytest.fixture()
def chain(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Every expensive stage faked; the collection child is supplied by the test.

    Unlike ``recorder`` the notification is left real, so a test can read the
    text the job seeker is actually shown.
    """
    from dreamjob.pipeline import autopilot

    calls: dict = {"order": [], "opportunities": 7, "rescored": 0}
    monkeypatch.setattr(autopilot, "_build_composite", lambda s, o: {"id": "comp1", "version": 1})
    monkeypatch.setattr(autopilot, "_build_dream_job", lambda s, o: {"id": "dream1"})
    monkeypatch.setattr(
        autopilot, "_save_proposed_directives", lambda s, o: ("dir1", "Autopilot directives")
    )
    monkeypatch.setattr(autopilot, "_plan", lambda c, s, o: {"sources": 4})
    monkeypatch.setattr(autopilot, "_profile_companies", _fake_profile(calls))
    monkeypatch.setattr(autopilot, "_rescore", _fake_rescore(calls))
    monkeypatch.setattr(
        autopilot,
        "_opportunity_counts",
        lambda c: {
            "opportunities": calls["opportunities"],
            "speculative": 0,
            "scored": calls["opportunities"],
        },
    )
    return calls


def _launch(chain: dict, child_id: str):
    async def launch(campaign_id, seeker_id):
        chain["order"].append("collection_start")
        return child_id

    return launch


def _settle_with_runner(ctx) -> None:
    """Run the worker through the real runner so the job row is settled."""
    from dreamjob.pipeline import autopilot

    asyncio.run(runner._run(ctx, autopilot.autopilot_worker))


def test_a_failed_collection_child_settles_the_autopilot_job_failed(
    seeker, profile, chain, monkeypatch
):
    """D1: a child that did not finish is a failure, child error included."""
    from dreamjob.pipeline import autopilot

    campaign_id = _campaign_row(seeker)
    child = _collection_child(
        seeker,
        campaign_id,
        status="failed",
        last_error="SourceUnavailable: 3 request(s) timed out",
        error_count=1,
    )
    monkeypatch.setattr(
        autopilot, "_create_campaign", lambda s, c, o: (campaign_id, "Autopilot run")
    )
    monkeypatch.setattr(autopilot, "_start_collection", _launch(chain, child))

    ctx = _make_ctx(seeker)
    _settle_with_runner(ctx)

    row = query_one("SELECT status, last_error FROM job_run WHERE id = ?", (ctx.job_id,))
    assert row["status"] == "failed", "the autopilot job follows the child it waited on"
    assert "SourceUnavailable" in row["last_error"]
    assert "blocked=" in row["last_error"], "the refused-source summary is in the reason"

    note = query_one(
        "SELECT body FROM notification WHERE job_seeker_id = ? AND kind = ?",
        (seeker, "autopilot_failed"),
    )
    assert note is not None, "the failure notification still fires"
    assert "SourceUnavailable" in note["body"], "and carries the child's real error"
    assert "profiling" not in chain["order"], "a failed collection never profiles"


def test_a_failed_campaign_fails_the_autopilot_even_when_the_child_finished(
    seeker, profile, chain, monkeypatch
):
    """D2: a ``done`` child under a ``failed`` campaign is not success."""
    from dreamjob.pipeline import autopilot

    campaign_id = _campaign_row(seeker, status="failed")
    child = _collection_child(
        seeker,
        campaign_id,
        status="done",
        stats={"records": 0, "errors": 1, "blocked": 0, "gone": 0},
        last_error="board.stepstone: The read operation timed out",
        error_count=1,
    )
    monkeypatch.setattr(
        autopilot, "_create_campaign", lambda s, c, o: (campaign_id, "Autopilot run")
    )
    monkeypatch.setattr(autopilot, "_start_collection", _launch(chain, child))

    ctx = _make_ctx(seeker)
    _settle_with_runner(ctx)

    row = query_one("SELECT status, last_error FROM job_run WHERE id = ?", (ctx.job_id,))
    assert row["status"] == "failed"
    assert "campaign status failed" in row["last_error"]
    assert "timed out" in row["last_error"]
    assert "profiling" not in chain["order"]


@pytest.mark.parametrize("status", ["cancelled", "failed"])
def test_a_terminal_child_that_is_not_done_fails(seeker, status):
    from dreamjob.pipeline import autopilot

    campaign_id = _campaign_row(seeker)
    child = _collection_child(seeker, campaign_id, status=status)
    ctx = _make_ctx(seeker)

    with pytest.raises(autopilot.AutopilotError, match="ended"):
        asyncio.run(autopilot._await_collection(ctx, child, campaign_id))


def test_a_vanished_child_fails(seeker):
    from dreamjob.pipeline import autopilot

    ctx = _make_ctx(seeker)
    with pytest.raises(autopilot.AutopilotError, match="vanished"):
        asyncio.run(autopilot._await_collection(ctx, "no-such-collection", ""))


def test_zero_records_with_blocked_sources_stays_done_and_says_why(
    seeker, profile, chain, monkeypatch
):
    """A search that completed and found nothing is not a failure."""
    from dreamjob.pipeline import autopilot

    # Collection writes the campaign row ``failed`` when nothing was collected
    # at all and any source ran; every source declining us is that shape.
    campaign_id = _campaign_row(seeker, status="failed")
    child = _collection_child(
        seeker,
        campaign_id,
        stats={
            "records": 0,
            "errors": 0,
            "blocked": 2,
            "gone": 1,
            "by_adapter": {
                "board.indeed": {"pages": 1, "records": 0, "errors": 0, "blocked": 1, "gone": 0},
                "board.jobat": {"pages": 1, "records": 0, "errors": 0, "blocked": 1, "gone": 1},
            },
        },
        last_error="2 request(s) refused and nothing collected - HTTP 403",
    )
    monkeypatch.setattr(
        autopilot, "_create_campaign", lambda s, c, o: (campaign_id, "Autopilot run")
    )
    monkeypatch.setattr(autopilot, "_start_collection", _launch(chain, child))
    chain["opportunities"] = 0

    ctx = _make_ctx(seeker)
    _settle_with_runner(ctx)

    row = query_one("SELECT status, checkpoint FROM job_run WHERE id = ?", (ctx.job_id,))
    assert row["status"] == "done", "declined sources with nothing collected is not a failure"
    report = from_json(row["checkpoint"])["report"]
    assert report["reason"] == "no_records"
    assert report["sources"]["blocked"] == 2
    assert report["sources"]["gone"] == 1
    assert report["sources"]["adapters"]["board.indeed"]["blocked"] == 1
    assert report["counts"]["opportunities"] == 0

    note = query_one(
        "SELECT title FROM notification WHERE job_seeker_id = ? AND kind = ?",
        (seeker, "autopilot_ready"),
    )
    assert note is not None
    assert note["title"] == "Your search finished, but found nothing to rank", (
        "zero opportunities must not be announced as a ready shortlist"
    )


@pytest.mark.parametrize("status", ["running", "paused", "pending", "done"])
def test_a_resumed_run_reuses_a_usable_collection_child(seeker, status):
    from dreamjob.pipeline import autopilot

    campaign_id = _campaign_row(seeker)
    child = _collection_child(seeker, campaign_id, status=status)
    assert autopilot._reusable_collection({"collection_job_id": child}) == child


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_an_unusable_collection_child_is_replaced(seeker, status):
    from dreamjob.pipeline import autopilot

    campaign_id = _campaign_row(seeker)
    child = _collection_child(seeker, campaign_id, status=status)
    assert autopilot._reusable_collection({"collection_job_id": child}) == ""


def test_a_resumed_run_adopts_an_existing_done_child_instead_of_launching(
    seeker, profile, chain, monkeypatch
):
    """D3: the id written before the wait is the child a resume continues."""
    from dreamjob.pipeline import autopilot

    campaign_id = _campaign_row(seeker)
    child = _collection_child(seeker, campaign_id, stats={"records": 2})
    monkeypatch.setattr(autopilot, "_start_collection", _launch(chain, child))

    waited: list[str] = []
    real_wait = autopilot._await_collection

    async def spy_wait(ctx, job_id, camp_id):
        waited.append(job_id)
        return await real_wait(ctx, job_id, camp_id)

    monkeypatch.setattr(autopilot, "_await_collection", spy_wait)

    ctx = _make_ctx(seeker)
    ctx.checkpoint.update(
        {
            "composite_id": "comp1",
            "dream_job_model_id": "dream1",
            "directive_set_id": "dir1",
            "campaign_id": campaign_id,
            "planned": True,
            "collection_job_id": child,
            "stage_index": 5,
        }
    )
    _settle_with_runner(ctx)

    assert "collection_start" not in chain["order"], "no second collection was launched"
    assert waited == [child], "the existing child was the one waited on"
    assert ctx.checkpoint["collected"] is True


def test_waiting_on_a_running_child_returns_when_it_finishes(seeker, monkeypatch):
    from dreamjob.pipeline import autopilot

    campaign_id = _campaign_row(seeker)
    child = _collection_child(seeker, campaign_id, status="running")
    ctx = _make_ctx(seeker)
    monkeypatch.setattr(autopilot, "POLL_SECONDS", 0.01)

    async def wait_then_finish():
        task = asyncio.create_task(autopilot._await_collection(ctx, child, campaign_id))
        await asyncio.sleep(0.05)
        update_row(
            "job_run",
            child,
            {"status": "done", "checkpoint": {"stats": {"records": 2}}},
        )
        return await task

    assessment = asyncio.run(wait_then_finish())
    assert assessment["outcome"] == "done"
    assert assessment["sources"]["records"] == 2


def test_cancelling_the_autopilot_cancels_the_collection_child(seeker):
    """D4: a stopped run stops producing opportunities too."""
    from dreamjob.pipeline import autopilot

    campaign_id = _campaign_row(seeker)
    child = _collection_child(seeker, campaign_id, status="running")
    ctx = _make_ctx(seeker)
    ctx._control.cancelled = True

    with pytest.raises(JobCancelled):
        asyncio.run(autopilot._await_collection(ctx, child, campaign_id))

    row = query_one("SELECT status FROM job_run WHERE id = ?", (child,))
    assert row["status"] == "cancelled"
