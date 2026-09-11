"""Autopilot: one action that carries a profile to a ranked shortlist.

The product owner's model of the product is three steps — create your profile,
review and select what came back, apply — with everything between them running
on its own. Before this module, every stage between those steps was a separate
button on a separate screen: enrich, synthesise, confirm the dream job,
propose directives, save directives, create a campaign, plan, launch. That is
eight decisions the product can make for itself.

This module is the missing orchestrator (FR-161..166, FR-181..186, FR-121..128,
FR-261..285). It is deliberately *thin*: every stage it runs already existed and
already has its own tests. What is new is only the order, the defaults, and the
promise that a failure in one stage is reported rather than swallowed.

The chain
---------
1. **Composite profile** (FR-121) — synthesise what the documents and any
   approved enrichment already established.
2. **Dream-job model** (FR-128) — turn the free-text statement into structure.
   Absent a statement this step is skipped, not failed: it is the one input a
   profile can legitimately be missing.
3. **Directives** (FR-147) — propose a full directive set from the profile and
   *save it*. The proposal function already existed; nothing ever called it on
   the job seeker's behalf, so the largest decision surface in the product sat
   between the user and their first result.
4. **Campaign** (FR-161) — create it from the saved directives.
5. **Plan** (FR-162) — per-source native queries, bounded by the defaults.
6. **Collection** (FR-181) — launch it. Ranking runs automatically at the end
   of collection already (see ``collection._rank_collected``), so the shortlist
   exists when collection returns.
7. **Company profiles and financials** (FR-221..246) for the companies that
   actually produced an opportunity, bounded by a limit.
8. **Notify** — one notification saying the shortlist is ready, so the user
   does not have to watch a progress bar.

Everything is tunable
---------------------
The defaults are opinionated but every one is a field on :class:`AutopilotOptions`
and can be overridden per run from the start screen. Nothing here is a new
consent: the CR-410 gate on sending profile data to the model provider is
checked by ``composite.build_composite`` itself, and autopilot refuses to start
without it rather than discovering the problem halfway through.

Nothing is sent
---------------
This module prepares. It never generates an application package and never sends
an email (NFR-305); between "ranked shortlist" and "outbound message" there is
still a human choosing opportunities and approving drafts.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass
from typing import Any

from dreamjob.db.connection import (
    from_json,
    query_one,
    to_json,
    update_row,
    utcnow,
)
from dreamjob.db.repositories import campaigns as campaign_repo
from dreamjob.db.repositories import directives as directives_repo
from dreamjob.db.repositories import pipeline_cards as notify_repo
from dreamjob.jobs.runner import JobContext, runner

log = logging.getLogger(__name__)

JOB_KIND = "autopilot"

#: How many stages the progress bar counts. Kept as a constant so the bar and
#: the worker cannot disagree about the denominator.
TOTAL_STEPS = 8

#: Company profiling and financials are the two most expensive optional passes,
#: so they are bounded by default to the companies that actually produced
#: something to apply to. A campaign that finds 200 companies does not then
#: crawl 200 websites before showing the shortlist.
DEFAULT_COMPANY_LIMIT = 12

#: The bounds autopilot puts on the campaign it creates (FR-186).
#:
#: These are deliberately far tighter than ``planning.DEFAULT_CAPS``. Those
#: caps are sized for an operator's deliberate, supervised data-gathering run;
#: an autopilot run is the opposite — a job seeker pressed one button and is
#: waiting for a shortlist. At the planning defaults a single campaign planned
#: 5,007 items ("one per company per vendor") and a 10,000-page budget, which
#: is neither reviewable nor finishable, and it starved the sources that
#: actually hold vacancies. Bounding the company inventory and the page budget
#: keeps a run to minutes and spends its budget on vacancies rather than on a
#: registry sweep of every company the knowledge base has ever seen.
DEFAULT_CAPS_OVERRIDE: dict[str, int] = {
    "max_companies": 40,
    "max_pages": 150,
    "max_pages_per_source": 8,
    "max_duration_seconds": 45 * 60,
}

#: A collection run is expected to take minutes; the autopilot waits for it so
#: company profiling can follow. The guard exists so a hung collection cannot
#: hold an autopilot job open forever - it gives up waiting, says so, and leaves
#: the shortlist that collection already produced.
COLLECTION_WAIT_TIMEOUT_SECONDS = 3 * 60 * 60
POLL_SECONDS = 2.0


@dataclass
class AutopilotOptions:
    """Everything the run needs, all defaulted, all overridable (FR-163)."""

    #: Company profiles and financials to build after collection, 0 to skip.
    company_limit: int = DEFAULT_COMPANY_LIMIT
    #: Use the model for planning and scoring; off makes a run deterministic.
    use_llm: bool = True
    #: Keep the dream-job model step even when no statement is written.
    require_dream_job: bool = False
    #: Cap on collection pages across all sources (FR-186).
    max_pages: int | None = None
    #: Cap on the companies the campaign plans against (FR-186).
    max_companies: int | None = None
    #: Campaign name; a sensible one is derived when omitted.
    campaign_name: str | None = None
    #: Name for the saved directive set.
    directive_name: str | None = None

    @classmethod
    def from_checkpoint(cls, checkpoint: dict | None) -> AutopilotOptions:
        raw = (checkpoint or {}).get("options") or {}
        if isinstance(raw, str):
            raw = from_json(raw, {}) or {}
        allowed = set(cls.__annotations__)
        return cls(**{k: v for k, v in raw.items() if k in allowed})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Starting a run
# ---------------------------------------------------------------------------


async def start(
    job_seeker_id: str,
    *,
    options: AutopilotOptions | None = None,
    campaign_id: str | None = None,
) -> str:
    """Create and start an autopilot job.  Returns the ``job_run`` id.

    The caller is responsible for having checked consent; :func:`preflight`
    does that and returns the same refusal the start screen shows.
    """
    options = options or AutopilotOptions()
    checkpoint = {"options": options.to_dict()}
    if campaign_id:
        checkpoint["campaign_id"] = campaign_id

    job_id = runner.create(
        JOB_KIND,
        job_seeker_id=job_seeker_id,
        total=TOTAL_STEPS,
        estimated_seconds=900,
    )
    update_row("job_run", job_id, {"checkpoint": to_json(checkpoint)})
    # The module-level worker is named explicitly so a resumed job (after a
    # restart) uses the same function without a closure to re-create.
    await runner.start(job_id, autopilot_worker)
    record_started(job_seeker_id, job_id)
    return job_id


def preflight(job_seeker_id: str) -> dict[str, Any]:
    """What autopilot can and cannot do for this job seeker, before starting.

    Returned rather than raised so the start screen can explain the situation
    and offer the fix (import a document, record consent) instead of showing an
    error. ``ready`` is the only field the start button needs.
    """
    from dreamjob.pipeline import composite as composite_mod  # noqa: PLC0415
    from dreamjob.security.auth_service import has_consent  # noqa: PLC0415

    profile = directives_repo.latest_profile_version(job_seeker_id)
    consent = has_consent(job_seeker_id, "llm_transfer")
    statements = {
        "profile": bool(profile),
        "consent": bool(consent),
        "dream_job_statement": bool((profile or {}).get("dream_job_statement")),
    }

    blockers: list[dict[str, str]] = []
    if not profile:
        blockers.append(
            {
                "code": "no_profile",
                "message": "Import your LinkedIn export or CV first — there is nothing to work from.",
                "screen": "/profile",
            }
        )
    if profile and not consent:
        blockers.append(
            {
                "code": "no_consent",
                "message": (
                    "Autopilot reads your profile with the AI, which sends it outside the "
                    "EU. Record that consent to continue."
                ),
                "screen": "/composite",
            }
        )
    # A missing dream-job statement is a warning, not a blocker: autopilot can
    # still rank on the profile alone, it just matches less well.
    warnings = []
    if profile and not statements["dream_job_statement"]:
        warnings.append(
            {
                "code": "no_statement",
                "message": (
                    "No dream-job statement yet, so matching uses your profile alone. "
                    "Adding one improves the ranking."
                ),
                "screen": "/dream-job",
            }
        )

    return {
        "ready": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "signals": statements,
        "composite_available": composite_mod.has_consent_for(job_seeker_id, "llm_transfer"),
    }


def latest_for_seeker(job_seeker_id: str) -> dict | None:
    """The most recent autopilot run, for the journey screen to resume from."""
    row = query_one(
        "SELECT * FROM job_run WHERE job_seeker_id = ? AND kind = ? ORDER BY created_at DESC LIMIT 1",
        (job_seeker_id, JOB_KIND),
    )
    if row is None:
        return None
    row = dict(row)
    row["checkpoint"] = from_json(row.get("checkpoint"), {}) or {}
    return row


# ---------------------------------------------------------------------------
# The worker
# ---------------------------------------------------------------------------


def _stage(ctx: JobContext, done: int, label: str) -> None:
    ctx.progress(done, TOTAL_STEPS)
    ctx.save_checkpoint(stage=label, stage_index=done)


def _save(ctx: JobContext, checkpoint: dict, report: dict) -> None:
    """Persist progress, report included.

    The report is written *into* the checkpoint rather than passed alongside
    it: on a resumed run the loaded checkpoint already contains a ``report``
    key, so ``save_checkpoint(**checkpoint, report=report)`` would pass it
    twice and fail.
    """
    checkpoint["report"] = report
    ctx.save_checkpoint(**checkpoint)


async def autopilot_worker(ctx: JobContext):
    """Run the chain.  An async generator so the runner can pause and cancel it.

    The generator yields between stages; ``JobContext.checkpoint_barrier`` is
    what actually honours a pause or a cancel, and it runs inside the runner's
    iteration, so yielding after each stage is what makes the whole run
    interruptible rather than only the stages that happen to await.
    """
    seeker_id = ctx.job_seeker_id
    if not seeker_id:
        raise ValueError("Autopilot needs a job seeker")

    options = AutopilotOptions.from_checkpoint(ctx.checkpoint)
    checkpoint = dict(ctx.checkpoint)
    done = int(checkpoint.get("stage_index") or 0)
    report: dict[str, Any] = dict(checkpoint.get("report") or {})

    # --- 1. composite profile ---------------------------------------------
    if "composite_id" not in checkpoint:
        _stage(ctx, done, "composite")
        yield
        composite = await asyncio.to_thread(_build_composite, seeker_id, options)
        checkpoint["composite_id"] = composite.get("id")
        report["composite"] = {"id": composite.get("id"), "version": composite.get("version")}
        _save(ctx, checkpoint, report)
        done = 1

    # --- 2. dream-job model (optional) ------------------------------------
    if "dream_job_model_id" not in checkpoint:
        _stage(ctx, done, "dream_job")
        yield
        dream = await asyncio.to_thread(_build_dream_job, seeker_id, options)
        checkpoint["dream_job_model_id"] = (dream or {}).get("id")
        report["dream_job"] = {
            "id": (dream or {}).get("id"),
            "skipped": dream is None,
        }
        _save(ctx, checkpoint, report)
        done = 2

    # --- 3. directives: propose and SAVE ----------------------------------
    if "directive_set_id" not in checkpoint:
        _stage(ctx, done, "directives")
        yield
        directive_set_id, directive_name = await asyncio.to_thread(
            _save_proposed_directives, seeker_id, options
        )
        checkpoint["directive_set_id"] = directive_set_id
        report["directives"] = {"id": directive_set_id, "name": directive_name}
        _save(ctx, checkpoint, report)
        done = 3

    # --- 4. campaign ------------------------------------------------------
    if "campaign_id" not in checkpoint:
        _stage(ctx, done, "campaign")
        yield
        campaign_id, campaign_name = await asyncio.to_thread(
            _create_campaign, seeker_id, checkpoint, options
        )
        checkpoint["campaign_id"] = campaign_id
        report["campaign"] = {"id": campaign_id, "name": campaign_name}
        _save(ctx, checkpoint, report)
        done = 4

    campaign_id = checkpoint["campaign_id"]

    # --- 5. plan ----------------------------------------------------------
    if not checkpoint.get("planned"):
        _stage(ctx, done, "plan")
        yield
        plan = await asyncio.to_thread(_plan, campaign_id, seeker_id, options)
        checkpoint["planned"] = True
        report["plan"] = plan
        _save(ctx, checkpoint, report)
        done = 5

    # --- 6. collection (ranking runs inside it) ---------------------------
    if not checkpoint.get("collected"):
        _stage(ctx, done, "collection")
        yield
        collection_job_id = await _start_collection(campaign_id, seeker_id)
        checkpoint["collection_job_id"] = collection_job_id
        _save(ctx, checkpoint, report)

        finished = await _await_collection(ctx, collection_job_id)
        report["collection"] = {"job_id": collection_job_id, "outcome": finished}
        if finished != "done":
            # Collection is load-bearing: without it there is nothing to rank,
            # so the run stops here and says why rather than continuing into
            # company profiling against an empty corpus.
            checkpoint["collected"] = finished
            _save(ctx, checkpoint, report)
            _finalise_notification(seeker_id, campaign_id, report, ok=False)
            return
        checkpoint["collected"] = True
        _save(ctx, checkpoint, report)
        done = 6

    # --- 7. company profiles and financials -------------------------------
    if not checkpoint.get("profiled"):
        _stage(ctx, done, "profiling")
        yield
        report["profiling"] = await _profile_companies(campaign_id, seeker_id, options)
        checkpoint["profiled"] = True
        _save(ctx, checkpoint, report)
        done = 7

    # --- 8. rescore and notify --------------------------------------------
    _stage(ctx, done, "notify")
    yield
    report["rescored"] = await asyncio.to_thread(_rescore, campaign_id, options)
    report["counts"] = _opportunity_counts(campaign_id)
    _save(ctx, checkpoint, report)
    _finalise_notification(seeker_id, campaign_id, report, ok=True)
    ctx.progress(TOTAL_STEPS, TOTAL_STEPS)


# ---------------------------------------------------------------------------
# Stage implementations (kept small and separately typed so they can be tested)
# ---------------------------------------------------------------------------


def _build_composite(job_seeker_id: str, options: AutopilotOptions) -> dict:
    from dreamjob.pipeline import composite as composite_mod  # noqa: PLC0415

    # include_enrichment=None lets the composite module apply the FR-126
    # setting itself; autopilot does not get to overrule the job seeker's
    # choice to keep enrichment off.
    return composite_mod.build_composite(job_seeker_id, include_enrichment=None)


def _build_dream_job(job_seeker_id: str, options: AutopilotOptions) -> dict | None:
    from dreamjob.pipeline import dreamjob_model  # noqa: PLC0415

    try:
        return dreamjob_model.build_dream_job_model(job_seeker_id)
    except dreamjob_model.StatementMissing:
        if options.require_dream_job:
            raise
        log.info("Autopilot: no dream-job statement for %s; continuing on the profile alone", job_seeker_id)
        return None


def _save_proposed_directives(
    job_seeker_id: str, options: AutopilotOptions
) -> tuple[str, str]:
    """Propose directives from the profile and persist them (FR-147, FR-148).

    This is the step that used to be a decision the user had to make before
    they could see a single result. The proposal is deterministic and traced to
    profile fields (CR-405); saving it gives the campaign something to plan
    with, and the user can edit or replace it at any time.
    """
    from dreamjob.pipeline.directives import propose_directives  # noqa: PLC0415

    composite = directives_repo.latest_composite_profile(job_seeker_id)
    dream = directives_repo.latest_dream_job_model(job_seeker_id)
    profile = directives_repo.latest_profile_version(job_seeker_id)

    name = options.directive_name or _default_directive_name(composite, profile)
    proposal = propose_directives(composite, dream, profile, name=name)
    payload = proposal.directives
    payload.name = name
    # Keep the provenance the proposal computed so the directive screen can
    # still show where every value came from (FR-147).
    notes = payload.notes_to_ai or ""
    marker = "Proposed automatically from the profile (FR-147)."
    payload.notes_to_ai = f"{notes}\n{marker}".strip() if notes else marker
    directive_set_id = directives_repo.create(job_seeker_id, payload)
    return directive_set_id, name


def _default_directive_name(composite: dict | None, profile: dict | None) -> str:
    roles = from_json((composite or {}).get("core_competencies"), []) or []
    if isinstance(roles, list) and roles:
        first = roles[0]
        label = first.get("label") if isinstance(first, dict) else str(first)
        if label:
            return f"Autopilot — {str(label)[:60]}"
    return "Autopilot directives"


def _create_campaign(
    job_seeker_id: str, checkpoint: dict, options: AutopilotOptions
) -> tuple[str, str]:
    composite = directives_repo.latest_composite_profile(job_seeker_id)
    dream = directives_repo.latest_dream_job_model(job_seeker_id)
    profile = directives_repo.latest_profile_version(job_seeker_id)
    if not profile:
        raise ValueError("No profile version to base a campaign on")

    name = options.campaign_name or _default_campaign_name(profile, composite)
    from dreamjob.config import get_settings  # noqa: PLC0415
    from dreamjob.pipeline import planning  # noqa: PLC0415

    caps = dict(planning.DEFAULT_CAPS)
    # Tighten the defaults for an unattended run; the user can widen them per
    # run from the start screen (FR-186, FR-163).
    caps.update(DEFAULT_CAPS_OVERRIDE)
    if options.max_companies is not None:
        caps["max_companies"] = int(options.max_companies)
    if options.max_pages:
        caps["max_pages"] = int(options.max_pages)
    values: dict[str, Any] = {
        "name": name,
        "directive_set_id": checkpoint["directive_set_id"],
        "profile_version_id": profile["id"],
        "persona_id": profile.get("persona_id"),
        "composite_profile_id": (composite or {}).get("id"),
        "dream_job_model_id": (dream or {}).get("id"),
        "token_budget": get_settings().default_token_budget,
        "caps": caps,
        "status": "draft",
    }
    campaign_id = campaign_repo.create_campaign(job_seeker_id, values)
    campaign_repo.update_campaign(campaign_id, {"started_at": utcnow()})
    campaign_repo.record_audit(
        "campaign.created",
        job_seeker_id=job_seeker_id,
        actor="autopilot",
        entity_type="campaign",
        entity_id=campaign_id,
        detail={"name": name, "source": "autopilot"},
    )
    return campaign_id, name


def _default_campaign_name(profile: dict, composite: dict | None) -> str:
    from datetime import UTC, datetime  # noqa: PLC0415

    stamp = datetime.now(UTC).strftime("%d %b")
    return f"Autopilot {stamp}"


def _plan(campaign_id: str, job_seeker_id: str, options: AutopilotOptions) -> dict:
    from dreamjob.pipeline import planning  # noqa: PLC0415

    summary = planning.generate_plan(
        campaign_id, job_seeker_id, use_llm=options.use_llm
    )
    if not isinstance(summary, dict):
        return {"result": str(summary)}
    return {
        "sources": len(summary.get("items") or summary.get("plan") or []),
        "estimated_pages": summary.get("estimated_pages"),
        "reuse": summary.get("reuse") or summary.get("reuse_report"),
    }


async def _start_collection(campaign_id: str, job_seeker_id: str) -> str:
    from dreamjob.pipeline import collection  # noqa: PLC0415

    return await collection.launch(campaign_id, job_seeker_id)


async def _await_collection(ctx: JobContext, collection_job_id: str) -> str:
    """Wait for a collection job to reach a terminal state.

    Polls rather than subscribing because the job runner keeps its state in the
    database, which is also what lets this survive a restart: a resumed
    autopilot reads the same ``job_run`` row and keeps waiting.
    """
    waited = 0.0
    while waited < COLLECTION_WAIT_TIMEOUT_SECONDS:
        row = runner.status(collection_job_id)
        if row is None:
            return "vanished"
        status = row.get("status")
        if status in ("done", "failed", "cancelled"):
            return status
        # A pause or cancel of the autopilot also stops the wait.
        await ctx.checkpoint_barrier()
        await asyncio.sleep(POLL_SECONDS)
        waited += POLL_SECONDS
    log.warning("Autopilot gave up waiting for collection %s", collection_job_id)
    return "timed_out"


async def _profile_companies(
    campaign_id: str, job_seeker_id: str, options: AutopilotOptions
) -> dict:
    """Company profiles and financials for the companies actually in the list.

    Both passes are optional and both degrade: a missing registry key or an
    unreachable website is recorded, not fatal.
    """
    if options.company_limit <= 0:
        return {"skipped": True, "reason": "company_limit is 0"}

    result: dict[str, Any] = {}
    from dreamjob.pipeline import company_profile, financial  # noqa: PLC0415

    try:
        result["profiles"] = _summarise(
            await company_profile.rerun(
                campaign_id, job_seeker_id, limit=options.company_limit
            )
        )
    except Exception as exc:  # noqa: BLE001 - an optional pass must not fail the run
        log.exception("Autopilot: company profiling failed")
        result["profiles"] = {"error": str(exc)[:300]}

    try:
        result["financials"] = _summarise(
            await financial.rerun(
                campaign_id,
                job_seeker_id,
                limit=options.company_limit,
                collect=options.use_llm,
            )
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("Autopilot: financial analysis failed")
        result["financials"] = {"error": str(exc)[:300]}

    return result


def _summarise(result: Any) -> Any:
    """Keep the report small enough to store in a job checkpoint."""
    if isinstance(result, dict):
        keep = ("profiled", "built", "refreshed", "skipped", "failed", "analysed", "companies")
        return {k: result[k] for k in keep if k in result}
    return str(result)[:300]


def _rescore(campaign_id: str, options: AutopilotOptions) -> dict:
    """Re-rank once the company signals and financials exist (FR-281)."""
    from dreamjob.pipeline import scoring  # noqa: PLC0415

    campaign = campaign_repo.get_campaign_any(campaign_id)
    if campaign is None:
        return {"error": "campaign vanished"}
    report = scoring.score_campaign(campaign, use_llm=options.use_llm)
    if hasattr(report, "as_dict"):
        data = report.as_dict()
    elif isinstance(report, dict):
        data = report
    else:
        return {}
    return {
        "scored": data.get("scored") or data.get("count") or data.get("total"),
    }


def _opportunity_counts(campaign_id: str) -> dict:
    row = query_one(
        "SELECT COUNT(*) AS total, "
        "       SUM(CASE WHEN kind = 'speculative' THEN 1 ELSE 0 END) AS speculative, "
        "       SUM(CASE WHEN score IS NOT NULL THEN 1 ELSE 0 END) AS scored "
        "FROM opportunity WHERE campaign_id = ?",
        (campaign_id,),
    ) or {}
    return {
        "opportunities": int(row.get("total") or 0),
        "speculative": int(row.get("speculative") or 0),
        "scored": int(row.get("scored") or 0),
    }


def _finalise_notification(
    job_seeker_id: str, campaign_id: str, report: dict, *, ok: bool
) -> None:
    if ok:
        counts = report.get("counts") or _opportunity_counts(campaign_id)
        n = counts.get("opportunities", 0)
        title = (
            f"Your shortlist is ready — {n} opportunit{'y' if n == 1 else 'ies'}"
            if n
            else "Your search finished, but found nothing to rank"
        )
        body = (
            "Autopilot read your profile, searched the sources it judged relevant and "
            "ranked what came back. Open the ranked list to choose what to pursue."
        )
        notify_repo.notify(
            job_seeker_id,
            {
                "kind": "autopilot_ready",
                "severity": "info",
                "title": title,
                "body": body,
                "payload": {"campaign_id": campaign_id, "counts": counts},
                "dedup_key": f"autopilot-ready-{campaign_id}",
            },
        )
    else:
        notify_repo.notify(
            job_seeker_id,
            {
                "kind": "autopilot_failed",
                "severity": "warning",
                "title": "Autopilot could not finish the search",
                "body": (
                    "The collection stage did not complete, so there was nothing to rank. "
                    "Open the campaign to see what happened; the parts that did run are kept."
                ),
                "payload": {"campaign_id": campaign_id, **report.get("collection", {})},
                "dedup_key": f"autopilot-failed-{campaign_id}",
            },
        )


def record_started(job_seeker_id: str, job_id: str) -> None:
    campaign_repo.record_audit(
        "autopilot.started",
        job_seeker_id=job_seeker_id,
        actor=job_seeker_id,
        entity_type="job_run",
        entity_id=job_id,
    )


# The worker is registered at import time, as every other job kind is.
runner.register_worker(JOB_KIND, autopilot_worker)
