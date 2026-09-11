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
from dreamjob.db.connection import insert_row, utcnow
from dreamjob.db.repositories import seekers as seeker_repo


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

    async def wait(ctx, job_id):
        calls["order"].append(f"collection_wait:{outcome}")
        return outcome

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
    _drain(ctx)

    # Profiling never ran: there was nothing to profile against.
    assert "profiling" not in recorder["order"]
    assert recorder["order"][-1] == "notify_failed"
    assert recorder["notifications"][-1]["ok"] is False


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
